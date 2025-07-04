from flask import Flask, render_template, request, redirect, url_for, session, flash
from datetime import datetime, timedelta 
from functools import wraps
import os 
import boto3
from botocore.exceptions import ClientError

app = Flask(__name__)


# --- AWS Configuration ---
# These variables should be set in your EC2 instance's environment or via a deployment script.
AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1') # Default to us-east-1 if not set
USERS_TABLE = os.environ.get('USERS_TABLE', 'MedConnectUsers')
APPOINTMENTS_TABLE = os.environ.get('APPOINTMENTS_TABLE', 'MedConnectAppointments')
MEDICAL_RECORDS_TABLE = os.environ.get('MEDICAL_RECORDS_TABLE', 'MedConnectMedicalRecords')
# Ensure these SNS Topic ARNs are correct for your AWS account and region
PATIENT_SNS_TOPIC_ARN = os.environ.get('PATIENT_SNS_TOPIC_ARN', 'arn:aws:sns:us-east-1:123456789012:MedConnectPatientNotifications') # REPLACE WITH YOUR ACTUAL ARN
DOCTOR_SNS_TOPIC_ARN = os.environ.get('DOCTOR_SNS_TOPIC_ARN', 'arn:aws:sns:us-east-1:123456789012:MedConnectDoctorNotifications') # REPLACE WITH YOUR ACTUAL ARN

# --- AWS Clients ---
dynamodb = None
sns_client = None
users_table = None
appointments_table = None
medical_records_table = None

try:
    # boto3 will automatically use credentials from an attached IAM Role on EC2
    # or from environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY) / AWS CLI config locally.
    dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)
    sns_client = boto3.client('sns', region_name=AWS_REGION)
    
    users_table = dynamodb.Table(USERS_TABLE)
    appointments_table = dynamodb.Table(APPOINTMENTS_TABLE)
    medical_records_table = dynamodb.Table(MEDICAL_RECORDS_TABLE)

    # Test DynamoDB connection by attempting to access a table (e.g., describe table)
    users_table.load() 
    appointments_table.load()
    medical_records_table.load()
    print(f"Successfully connected to AWS DynamoDB in region {AWS_REGION} and SNS.")

except ClientError as e:
    print(f"AWS Client Error: {e}. Check IAM Role permissions, AWS region, and service availability.")
    dynamodb, sns_client = None, None # Set to None to indicate connection failure
    users_table, appointments_table, medical_records_table = None, None, None
except Exception as e:
    print(f"General AWS Connection Error: {e}. Ensure boto3 is installed and configured.")
    dynamodb, sns_client = None, None
    users_table, appointments_table, medical_records_table = None, None, None

# --- Helper Functions ---
def _send_sns_message(topic_arn, subject, message, target_email=None):
    """Sends an SNS message to a topic or directly to an email endpoint."""
    if sns_client is None or topic_arn is None:
        print("SNS client not initialized or topic ARN missing. Cannot send notification.")
        return False
    try:
        # For direct email publishing, the email address needs to be subscribed to the topic.
        # We publish to the topic, and the topic's subscriptions handle delivery.
        response = sns_client.publish(
            TopicArn=topic_arn,
            Subject=subject,
            Message=message
        )
        print(f"SNS message sent: {response['MessageId']}")
        return True
    except ClientError as e:
        print(f"Failed to send SNS message: {e}")
        return False
    except Exception as e:
        print(f"An unexpected error occurred while sending SNS message: {e}")
        return False

def _update_session_user_data(user_email):
    """Refreshes the user's session data from DynamoDB."""
    if users_table is None: return False 
    try:
        response = users_table.get_item(Key={'email': user_email})
        user = response.get('Item')
        if not user: return False

        session['user_data'] = {'email': user['email'], 'name': user['name'], 'user_type': user['user_type']}
        profile_key = 'patient_profile' if user['user_type'] == 'patient' else 'doctor_profile'
        if profile_key in user: session['user_data'].update(user[profile_key])
        return True
    except ClientError as e:
        print(f"DynamoDB Error in _update_session_user_data: {e}")
        return False
    except Exception as e:
        print(f"Error in _update_session_user_data: {e}")
        return False

def login_required(user_type=None):
    def wrapper(fn):
        @wraps(fn)
        def decorated_view(*args, **kwargs):
            if any(col is None for col in [users_table, appointments_table, medical_records_table]):
                flash('Database connection failed. Please contact support.', 'error')
                return redirect(url_for('index')) 
            if not session.get('logged_in'):
                flash('Please login to access this page.', 'error')
                return redirect(url_for('login'))
            if user_type and session.get('user_type') != user_type:
                flash(f'Unauthorized: You must be a {user_type} to access this page.', 'error')
                return redirect(url_for(f'{session.get("user_type")}_dashboard') if session.get('user_type') in ['patient', 'doctor'] else url_for('index'))
            return fn(*args, **kwargs)
        return decorated_view
    return wrapper

def generate_unique_id(prefix, ids_string):
    """Generates a unique string ID."""
    return f"{prefix}_{ids_string.replace('@', '_').replace('.', '_').replace('-', '_')}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

# --- Public Routes ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if users_table is None:
        flash('Database connection not established. Cannot log in.', 'error')
        return render_template('login.html', error='Database connection error.')
    if request.method == 'POST':
        email, password = request.form['loginEmail'], request.form['loginPassword']
        try:
            response = users_table.get_item(Key={'email': email})
            user = response.get('Item')
            if user and user['password'] == password: 
                session.update({'logged_in': True, 'user_type': user['user_type'], 'user_email': user['email']})
                _update_session_user_data(user['email'])
                return redirect(url_for(f"{user['user_type']}_dashboard"))
            return render_template('login.html', error='Invalid email or password.')
        except ClientError as e:
            print(f"DynamoDB Error during login: {e}")
            return render_template('login.html', error='Database error during login.')
    return render_template('login.html', **request.args)

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if users_table is None:
        flash('Database connection not established. Cannot sign up.', 'error')
        return render_template('signup.html', error='Database connection error.')
    if request.method == 'POST':
        name, email, password, confirm_password, user_type = request.form['signupName'], request.form['signupEmail'], request.form['signupPassword'], request.form['confirmPassword'], request.form.get('user_type')
        if not all([name, email, password, confirm_password, user_type]) or password != confirm_password:
            return render_template('signup.html', error='All fields are required and passwords must match.')
        try:
            response = users_table.get_item(Key={'email': email})
            if response.get('Item'):
                return render_template('signup.html', error='Email already registered. Please login or use a different email.')

            new_user_document = {'email': email, 'password': password, 'user_type': user_type, 'name': name}
            if user_type == 'patient':
                dob, gender = request.form.get('dateOfBirth'), request.form.get('gender')
                if not all([dob, gender]): return render_template('signup.html', error='Patient details (Date of Birth, Gender) are required.')
                new_user_document['patient_profile'] = {'dateOfBirth': dob, 'gender': gender}
            elif user_type == 'doctor':
                specialization, license = request.form.get('specialization'), request.form.get('licenseNumber')
                if not all([specialization, license]): return render_template('signup.html', error='Doctor details (Specialization, License Number) are required.')
                new_user_document['doctor_profile'] = {'specialization': specialization, 'licenseNumber': license, 'experience': 0, 'achievements': ''}
            else: return render_template('signup.html', error='Invalid user type selected.')

            users_table.put_item(Item=new_user_document)
            flash(f'Registration successful as {user_type.capitalize()}! Please login.', 'success')
            return redirect(url_for('login'))
        except ClientError as e:
            print(f"DynamoDB Error during signup: {e}")
            return render_template('signup.html', error='Database error during signup.')
    return render_template('signup.html', error=request.args.get('error'))

@app.route('/aboutus')
def aboutus(): return render_template('aboutus.html')

@app.route('/contactus', methods=['GET', 'POST'])
def contactus():
    if request.method == 'POST': flash('Your message has been sent!', 'success')
    return render_template('contactus.html')

# --- Patient Routes ---
@app.route('/patient_dashboard')
@login_required(user_type='patient')
def patient_dashboard():
    return render_template('patientdashboard.html', **session.get('user_data', {}))

@app.route('/patient_appointments')
@login_required(user_type='patient')
def patient_appointments():
    user_email = session['user_email']
    pending, upcoming, prescription_ready, past = [], [], [], []
    current_date_time = datetime.now() 

    try:
        # DynamoDB Scan with FilterExpression (less efficient for large tables, consider GSI for patientEmail)
        response = appointments_table.scan(
            FilterExpression=boto3.dynamodb.conditions.Attr('patientEmail').eq(user_email)
        )
        appointments = response.get('Items', [])
        
        for appt in appointments:
            try:
                appt_dt = datetime.strptime(f"{appt['date']} {appt['time']}", '%Y-%m-%d %H:%M')
                if appt['status'] == 'Pending': pending.append(appt)
                elif appt['status'] == 'Accepted': 
                    if appt_dt <= current_date_time:
                        appointments_table.update_item(
                            Key={'unique_appointment_id': appt['unique_appointment_id']},
                            UpdateExpression="SET #s = :status",
                            ExpressionAttributeNames={'#s': 'status'},
                            ExpressionAttributeValues={':status': 'Completed'}
                        )
                        appt['status'] = 'Completed' 
                    (past if appt['status'] == 'Completed' else upcoming).append(appt)
                elif appt['status'] == 'PrescriptionProvided': prescription_ready.append(appt)
                elif appt['status'] in ['Completed', 'Rejected']: past.append(appt)
            except ValueError: 
                print(f"Error parsing date/time for appointment {appt.get('unique_appointment_id')}. Placing in general active list.")
                (upcoming if appt.get('status') in ['Pending', 'Accepted', 'PrescriptionProvided'] else past).append(appt)
        
        # Sort lists after categorization
        pending.sort(key=lambda x: (x['date'], x['time']))
        upcoming.sort(key=lambda x: (x['date'], x['time']))
        prescription_ready.sort(key=lambda x: (x['date'], x['time']))
        past.sort(key=lambda x: (x['date'], x['time']), reverse=True) # Past usually sorted descending

    except ClientError as e:
        print(f"DynamoDB Error in patient_appointments: {e}")
        flash('Could not load appointments. Database error.', 'error')
    except Exception as e:
        print(f"Error in patient_appointments: {e}")
        flash('An unexpected error occurred.', 'error')
    
    _update_session_user_data(user_email)
    return render_template('patient_appointments.html', 
                           pending_appointments=pending, upcoming_appointments=upcoming, 
                           prescription_ready_appointments=prescription_ready, past_appointments=past)

@app.route('/view_patient_prescription/<string:unique_appointment_id_param>', methods=['GET'])
@login_required(user_type='patient')
def view_patient_prescription(unique_appointment_id_param):
    patient_email = session['user_email']
    try:
        response_appt = appointments_table.get_item(Key={'unique_appointment_id': unique_appointment_id_param})
        appointment = response_appt.get('Item')

        if not appointment or appointment['patientEmail'] != patient_email or appointment['status'] != 'PrescriptionProvided':
            flash('Prescription not found or already viewed/completed.', 'error')
            return redirect(url_for('patient_appointments'))
        
        response_record = medical_records_table.get_item(Key={'unique_appointment_id': unique_appointment_id_param})
        medical_record = response_record.get('Item')

        if not medical_record:
            flash('Medical record not found for this prescription.', 'error')
            appointments_table.update_item(
                Key={'unique_appointment_id': appointment['unique_appointment_id']},
                UpdateExpression="SET #s = :status",
                ExpressionAttributeNames={'#s': 'status'},
                ExpressionAttributeValues={':status': 'Completed'}
            )
            return redirect(url_for('patient_appointments'))
        
        appointments_table.update_item(
            Key={'unique_appointment_id': appointment['unique_appointment_id']},
            UpdateExpression="SET #s = :status",
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':status': 'Completed'}
        )
        flash('Appointment consultation completed. The details have been added to your medical history.', 'success')
        return render_template('view_prescription.html', appointment=appointment, medical_record=medical_record)
    except ClientError as e:
        print(f"DynamoDB Error in view_patient_prescription: {e}")
        flash('Database error while fetching prescription.', 'error')
        return redirect(url_for('patient_appointments'))

@app.route('/book_appointment/<string:doctor_email>', methods=['GET'])
@login_required(user_type='patient')
def book_appointment_page(doctor_email):
    try:
        response = users_table.get_item(Key={'email': doctor_email})
        doctor_user = response.get('Item')
        if not doctor_user or doctor_user.get('user_type') != 'doctor' or 'doctor_profile' not in doctor_user:
            flash('Doctor not found or invalid.', 'error')
            return redirect(url_for('doctor_list'))
        default_date = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
        return render_template('book_appointment.html', doctor_email=doctor_email, doctor_name=doctor_user['name'], 
                               doctor_specialization=doctor_user['doctor_profile']['specialization'],
                               default_date=default_date, default_time='09:00')
    except ClientError as e:
        print(f"DynamoDB Error in book_appointment_page: {e}")
        flash('Database error while fetching doctor details.', 'error')
        return redirect(url_for('doctor_list'))

@app.route('/book_appointment_submit', methods=['POST'])
@login_required(user_type='patient')
def book_appointment_submit():
    patient_email, doctor_email, appt_date_str, appt_time_str, issue_description = session['user_email'], request.form['doctorEmail'], request.form['appointmentDate'], request.form['appointmentTime'], request.form.get('issueDescription', '').strip()
    
    try:
        patient_response = users_table.get_item(Key={'email': patient_email})
        patient_user = patient_response.get('Item')
        doctor_response = users_table.get_item(Key={'email': doctor_email})
        doctor_user = doctor_response.get('Item')

        if not (patient_user and patient_user.get('patient_profile') and doctor_user and doctor_user.get('doctor_profile')):
            flash('Patient or Doctor profile not found.', 'error')
            return redirect(url_for('doctor_list'))
        
        booked_datetime = datetime.strptime(f"{appt_date_str} {appt_time_str}", '%Y-%m-%d %H:%M')
        if booked_datetime <= datetime.now() + timedelta(minutes=2):
            flash('Appointment must be at least 2 minutes in the future from now. Please choose a later time.', 'error')
            return redirect(url_for('book_appointment_page', doctor_email=doctor_email))
        if not issue_description:
            flash('Please describe your issue for the appointment.', 'error')
            return redirect(url_for('book_appointment_page', doctor_email=doctor_email))

        new_appointment = {
            'patientEmail': patient_email, 'doctorEmail': doctor_email, 'date': appt_date_str, 'time': appt_time_str, 'status': 'Pending', 
            'doctorName': doctor_user['name'], 'specialization': doctor_user['doctor_profile']['specialization'],
            'patientName': patient_user['name'], 'patientGender': patient_user['patient_profile']['gender'],
            'unique_appointment_id': generate_unique_id('appt', f"{patient_email}-{doctor_email}"), 'issueDescription': issue_description 
        }
        appointments_table.put_item(Item=new_appointment)
        
        # Notify doctor about new appointment
        _send_sns_message(
            DOCTOR_SNS_TOPIC_ARN,
            "New Appointment Request",
            f"Dr. {doctor_user['name']}, you have a new appointment request from {patient_user['name']} ({patient_email}) on {appt_date_str} at {appt_time_str} for: {issue_description}"
        )

        flash('Appointment booked successfully! Doctor will review your request.', 'success')
        return redirect(url_for('patient_appointments'))
    except ClientError as e:
        print(f"DynamoDB Error in book_appointment_submit: {e}")
        flash('Database error during appointment booking.', 'error')
        return redirect(url_for('book_appointment_page', doctor_email=doctor_email))
    except ValueError:
        flash('Invalid date or time format. Please useYYYY-MM-DD and HH:MM.', 'error')
        return redirect(url_for('book_appointment_page', doctor_email=doctor_email))
    except Exception as e:
        print(f"Error in book_appointment_submit: {e}")
        flash('An unexpected error occurred during booking.', 'error')
        return redirect(url_for('book_appointment_page', doctor_email=doctor_email))

@app.route('/complete_appointment/<string:unique_appointment_id_param>', methods=['POST'])
@login_required(user_type='patient')
def complete_appointment(unique_appointment_id_param):
    patient_email = session['user_email']
    try:
        response = appointments_table.get_item(Key={'unique_appointment_id': unique_appointment_id_param})
        appointment_to_update = response.get('Item')

        if not appointment_to_update or appointment_to_update['patientEmail'] != patient_email or appointment_to_update['status'] != 'Accepted':
            flash('Appointment not found, not in accepted status, or already completed.', 'error')
            return redirect(url_for('patient_appointments'))
        
        appointments_table.update_item(
            Key={'unique_appointment_id': unique_appointment_id_param},
            UpdateExpression="SET #s = :status",
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':status': 'Completed'}
        )
        
        record_response = medical_records_table.get_item(Key={'unique_appointment_id': unique_appointment_id_param})
        if not record_response.get('Item'): # Only insert if no record exists (doctor hasn't submitted yet)
            medical_records_table.put_item(Item={
                'recordType': 'Consultation Summary (Patient Marked)', 'date': appointment_to_update['date'],
                'doctorName': appointment_to_update['doctorName'], 'diagnosis': 'Patient Marked as Completed',
                'prescription': 'N/A', 'notes': f"Appointment with {appointment_to_update['doctorName']} on {appointment_to_update['date']} at {appointment_to_update['time']} marked completed by patient. Patient's initial issue: {appointment_to_update.get('issueDescription', 'N/A')}.", 
                'unique_appointment_id': unique_appointment_id_param, 'patientEmail': patient_email
            })
        flash(f"Appointment marked as completed!", 'success')
        return redirect(url_for('patient_appointments'))
    except ClientError as e:
        print(f"DynamoDB Error in complete_appointment: {e}")
        flash('Database error while completing appointment.', 'error')
        return redirect(url_for('patient_appointments'))

@app.route('/doctor_list')
@login_required(user_type='patient')
def doctor_list():
    doctors_available = []
    try:
        response = users_table.scan(
            FilterExpression=boto3.dynamodb.conditions.Attr('user_type').eq('doctor')
        )
        for doc_user in response.get('Items', []):
            if 'doctor_profile' in doc_user:
                doctors_available.append({
                    'email': doc_user['email'], 'name': doc_user['name'],
                    'specialization': doc_user['doctor_profile'].get('specialization', 'N/A'),
                    'licenseNumber': doc_user['doctor_profile'].get('licenseNumber', 'N/A'),
                    'experience': doc_user['doctor_profile'].get('experience', 0),
                    'achievements': doc_user['doctor_profile'].get('achievements', '')
                })
    except ClientError as e:
        print(f"DynamoDB Error in doctor_list: {e}")
        flash('Could not load doctors. Database error.', 'error')
    return render_template('doctor_list.html', doctors=doctors_available)

@app.route('/patient_medical_history')
@login_required(user_type='patient')
def patient_medical_history():
    user_email = session['user_email']
    medical_records = []
    try:
        response = medical_records_table.scan(
            FilterExpression=boto3.dynamodb.conditions.Attr('patientEmail').eq(user_email)
        )
        medical_records = sorted(response.get('Items', []), key=lambda x: x.get('date', ''), reverse=True)
    except ClientError as e:
        print(f"DynamoDB Error in patient_medical_history: {e}")
        flash('Could not load medical history. Database error.', 'error')
    return render_template('patient_medical_history.html', medical_records=medical_records)

# --- Doctor Routes ---
@app.route('/doctor_dashboard')
@login_required(user_type='doctor')
def doctor_dashboard():
    return render_template('doctordashboard.html', **session.get('user_data', {}))

@app.route('/accept_appointment/<string:unique_appointment_id_param>', methods=['POST'])
@login_required(user_type='doctor')
def accept_appointment(unique_appointment_id_param):
    doctor_email = session['user_email']
    try:
        response = appointments_table.get_item(Key={'unique_appointment_id': unique_appointment_id_param})
        appointment = response.get('Item')

        if not appointment or appointment['doctorEmail'] != doctor_email or appointment['status'] != 'Pending':
            flash('Appointment not found or not pending.', 'error')
        else:
            appointments_table.update_item(
                Key={'unique_appointment_id': unique_appointment_id_param},
                UpdateExpression="SET #s = :status",
                ExpressionAttributeNames={'#s': 'status'},
                ExpressionAttributeValues={':status': 'Accepted'}
            )
            # Notify patient about accepted appointment
            patient_user_response = users_table.get_item(Key={'email': appointment['patientEmail']})
            patient_user = patient_user_response.get('Item')
            if patient_user:
                _send_sns_message(
                    PATIENT_SNS_TOPIC_ARN,
                    "Appointment Accepted!",
                    f"Your appointment with Dr. {appointment['doctorName']} on {appointment['date']} at {appointment['time']} has been accepted."
                )
            flash('Appointment accepted successfully!', 'success')
        return redirect(url_for('doctor_appointments'))
    except ClientError as e:
        print(f"DynamoDB Error in accept_appointment: {e}")
        flash('Database error while accepting appointment.', 'error')
        return redirect(url_for('doctor_appointments'))

@app.route('/reject_appointment/<string:unique_appointment_id_param>', methods=['POST'])
@login_required(user_type='doctor')
def reject_appointment(unique_appointment_id_param):
    doctor_email = session['user_email']
    try:
        response = appointments_table.get_item(Key={'unique_appointment_id': unique_appointment_id_param})
        appointment = response.get('Item')

        if not appointment or appointment['doctorEmail'] != doctor_email or appointment['status'] != 'Pending':
            flash('Appointment not found or not pending.', 'error')
        else:
            appointments_table.update_item(
                Key={'unique_appointment_id': unique_appointment_id_param},
                UpdateExpression="SET #s = :status",
                ExpressionAttributeNames={'#s': 'status'},
                ExpressionAttributeValues={':status': 'Rejected'}
            )
            # Notify patient about rejected appointment
            patient_user_response = users_table.get_item(Key={'email': appointment['patientEmail']})
            patient_user = patient_user_response.get('Item')
            if patient_user:
                _send_sns_message(
                    PATIENT_SNS_TOPIC_ARN,
                    "Appointment Rejected",
                    f"Your appointment with Dr. {appointment['doctorName']} on {appointment['date']} at {appointment['time']} has been rejected. Please book another appointment."
                )
            flash('Appointment rejected.', 'info')
        return redirect(url_for('doctor_appointments'))
    except ClientError as e:
        print(f"DynamoDB Error in reject_appointment: {e}")
        flash('Database error while rejecting appointment.', 'error')
        return redirect(url_for('doctor_appointments'))

@app.route('/doctor_profile_settings', methods=['GET'])
@login_required(user_type='doctor')
def doctor_profile_settings():
    doctor_email = session['user_email']
    try:
        response = users_table.get_item(Key={'email': doctor_email})
        doctor_user = response.get('Item')
        if not doctor_user or doctor_user.get('user_type') != 'doctor' or 'doctor_profile' not in doctor_user:
            flash('Doctor profile not found.', 'error')
            return redirect(url_for('doctor_dashboard'))
        doctor_data = {
            'email': doctor_user['email'], 'name': doctor_user['name'],
            'specialization': doctor_user['doctor_profile'].get('specialization', ''),
            'licenseNumber': doctor_user['doctor_profile'].get('licenseNumber', ''),
            'experience': doctor_user['doctor_profile'].get('experience', 0),
            'achievements': doctor_user['doctor_profile'].get('achievements', '')
        }
        return render_template('doctor_profile_settings.html', doctor_data=doctor_data)
    except ClientError as e:
        print(f"DynamoDB Error in doctor_profile_settings: {e}")
        flash('Database error while fetching profile settings.', 'error')
        return redirect(url_for('doctor_dashboard'))

@app.route('/update_doctor_profile', methods=['POST'])
@login_required(user_type='doctor')
def update_doctor_profile():
    doctor_email = session['user_email']
    update_fields = {
        'name': request.form.get('name'), 'doctor_profile.specialization': request.form.get('specialization'),
        'doctor_profile.licenseNumber': request.form.get('licenseNumber'), 'doctor_profile.achievements': request.form.get('achievements')
    }
    try: 
        experience = int(request.form.get('experience', 0))
        update_fields['doctor_profile.experience'] = max(0, experience)
    except ValueError:
        flash('Experience must be a number.', 'error')
        return redirect(url_for('doctor_profile_settings'))
    
    try:
        # DynamoDB update_item with nested attributes
        update_expression = "SET #n = :name, #dp.#s = :spec, #dp.#l = :lic, #dp.#a = :ach"
        expression_attribute_names = {
            '#n': 'name', '#dp': 'doctor_profile', '#s': 'specialization', '#l': 'licenseNumber', '#a': 'achievements'
        }
        expression_attribute_values = {
            ':name': update_fields['name'], ':spec': update_fields['doctor_profile.specialization'],
            ':lic': update_fields['doctor_profile.licenseNumber'], ':ach': update_fields['doctor_profile.achievements']
        }
        if 'doctor_profile.experience' in update_fields:
            update_expression += ", #dp.#e = :exp"
            expression_attribute_names['#e'] = 'experience'
            expression_attribute_values[':exp'] = update_fields['doctor_profile.experience']

        users_table.update_item(
            Key={'email': doctor_email},
            UpdateExpression=update_expression,
            ExpressionAttributeNames=expression_attribute_names,
            ExpressionAttributeValues=expression_attribute_values
        )
        _update_session_user_data(doctor_email)
        flash('Profile updated successfully!', 'success')
        return redirect(url_for('doctor_profile_settings'))
    except ClientError as e:
        print(f"DynamoDB Error in update_doctor_profile: {e}")
        flash('Database error while updating profile.', 'error')
        return redirect(url_for('doctor_profile_settings'))

@app.route('/doctor_appointments')
@login_required(user_type='doctor')
def doctor_appointments():
    doctor_email = session['user_email']
    pending_doctor, upcoming_doctor, past_doctor = [], [], []
    current_date_time = datetime.now()

    try:
        # DynamoDB Scan with FilterExpression (less efficient for large tables, consider GSI for doctorEmail)
        response = appointments_table.scan(
            FilterExpression=boto3.dynamodb.conditions.Attr('doctorEmail').eq(doctor_email)
        )
        appointments = response.get('Items', [])

        for appt in appointments:
            try:
                appt_dt = datetime.strptime(f"{appt['date']} {appt['time']}", '%Y-%m-%d %H:%M')
                if appt['status'] == 'Pending': pending_doctor.append(appt)
                elif appt['status'] == 'Accepted':
                    if appt_dt <= current_date_time:
                        appointments_table.update_item(
                            Key={'unique_appointment_id': appt['unique_appointment_id']},
                            UpdateExpression="SET #s = :status",
                            ExpressionAttributeNames={'#s': 'status'},
                            ExpressionAttributeValues={':status': 'PrescriptionProvided'}
                        )
                        appt['status'] = 'PrescriptionProvided' 
                    (past_doctor if appt['status'] == 'PrescriptionProvided' else upcoming_doctor).append(appt)
                elif appt['status'] in ['PrescriptionProvided', 'Completed', 'Rejected']:
                    past_doctor.append(appt)
            except ValueError: 
                print(f"Error parsing date/time for doctor's appointment {appt.get('unique_appointment_id')}. Placing in general past list.")
                (pending_doctor if appt.get('status') == 'Pending' else past_doctor).append(appt)
        
        # Sort lists after categorization
        pending_doctor.sort(key=lambda x: (x['date'], x['time']))
        upcoming_doctor.sort(key=lambda x: (x['date'], x['time']))
        past_doctor.sort(key=lambda x: (x['date'], x['time']), reverse=True) # Past usually sorted descending

    except ClientError as e:
        print(f"DynamoDB Error in doctor_appointments: {e}")
        flash('Could not load appointments. Database error.', 'error')
    except Exception as e:
        print(f"Error in doctor_appointments: {e}")
        flash('An unexpected error occurred.', 'error')
    
    return render_template('doctor_appointments.html',
                           pending_appointments=pending_doctor, upcoming_appointments=upcoming_doctor,
                           past_appointments=past_doctor)

@app.route('/complete_doctor_appointment/<string:unique_appointment_id_param>/<string:patient_email>', methods=['GET'])
@login_required(user_type='doctor')
def complete_doctor_appointment(unique_appointment_id_param, patient_email):
    try:
        response_appt = appointments_table.get_item(Key={'unique_appointment_id': unique_appointment_id_param})
        current_appointment = response_appt.get('Item')

        if not current_appointment or current_appointment['doctorEmail'] != session['user_email'] or current_appointment['status'] != 'Accepted':
            flash('Appointment not found, not assigned to you, or not yet accepted.', 'error')
            return redirect(url_for('doctor_appointments'))
        
        response_record = medical_records_table.get_item(Key={'unique_appointment_id': unique_appointment_id_param})
        existing_record = response_record.get('Item')
        
        return render_template('doctor_consultation_form.html', 
                               appointment=current_appointment, patient_email=patient_email,
                               appointment_id=unique_appointment_id_param, existing_record=existing_record)
    except ClientError as e:
        print(f"DynamoDB Error in complete_doctor_appointment: {e}")
        flash('Database error while preparing consultation form.', 'error')
        return redirect(url_for('doctor_appointments'))

@app.route('/submit_consultation_notes', methods=['POST'])
@login_required(user_type='doctor')
def submit_consultation_notes():
    doctor_email, appt_id, patient_email = session['user_email'], request.form['appointmentId'], request.form['patientEmail']
    diagnosis, prescription, notes = request.form['diagnosis'], request.form.get('prescription', '').strip(), request.form['notes']

    try:
        doctor_response = users_table.get_item(Key={'email': doctor_email})
        doctor_user = doctor_response.get('Item')
        patient_response = users_table.get_item(Key={'email': patient_email})
        patient_user = patient_response.get('Item')

        if not (doctor_user and patient_user):
            flash('Doctor or Patient data not found.', 'error')
            return redirect(url_for('doctor_appointments'))
        
        appt_response = appointments_table.get_item(Key={'unique_appointment_id': appt_id})
        appointment_to_update = appt_response.get('Item')

        if not appointment_to_update or appointment_to_update['status'] != 'Accepted':
            flash('Appointment cannot be completed. It might have already been processed or its status changed.', 'error')
            return redirect(url_for('doctor_appointments'))

        appointments_table.update_item(
            Key={'unique_appointment_id': appt_id},
            UpdateExpression="SET #s = :status",
            ExpressionAttributeNames={'#s': 'status'},
            ExpressionAttributeValues={':status': 'PrescriptionProvided'}
        )

        medical_record_data = {
            'recordType': 'Consultation Summary', 'date': datetime.now().strftime('%Y-%m-%d'), 'doctorName': appointment_to_update['doctorName'], 
            'diagnosis': diagnosis, 'prescription': prescription, 'notes': notes, 'unique_appointment_id': appt_id, 'patientEmail': patient_email
        }
        medical_records_table.put_item(Item=medical_record_data) # Use put_item for upsert functionality

        print(f"Consultation notes and prescription added/updated for patient {patient_email} by doctor {doctor_email}. Status changed to PrescriptionProvided.")
        _send_sns_message(
            PATIENT_SNS_TOPIC_ARN,
            "Prescription Ready!",
            f"Your prescription from Dr. {doctor_user['name']} for your appointment on {appointment_to_update['date']} is now available. View it in your MedConnect appointments."
        )
        flash('Consultation notes and prescription successfully added! Patient can now view it.', 'success')
        return redirect(url_for('doctor_appointments'))
    except ClientError as e:
        print(f"DynamoDB Error in submit_consultation_notes: {e}")
        flash('Database error while submitting consultation notes.', 'error')
        return redirect(url_for('doctor_appointments'))

# --- Logout Route ---
@app.route('/logout')
def logout():
    session.clear()
    print("User logged out. Session cleared.")
    return redirect(url_for('index'))

# --- Doctor Patients Route ---
@app.route('/doctor_patients')
@login_required(user_type='doctor')
def doctor_patients():
    """Renders a list of patients seen by the logged-in doctor."""
    doctor_email = session['user_email']
    try:
        response = users_table.get_item(Key={'email': doctor_email})
        if not response.get('Item') or response.get('Item').get('user_type') != 'doctor':
            flash('Doctor profile not found.', 'error')
            return redirect(url_for('doctor_dashboard'))
        
        # Scan appointments to find all patients seen by this doctor, regardless of final status
        # In a real app, you'd use a GSI on doctorEmail for efficiency
        response = appointments_table.scan(
            FilterExpression=boto3.dynamodb.conditions.Attr('doctorEmail').eq(doctor_email)
        )
        all_doctor_appointments = response.get('Items', [])
        
        # Collect distinct patient emails from relevant appointment statuses
        distinct_patient_emails = {
            appt['patientEmail'] for appt in all_doctor_appointments 
            if appt['status'] in ['Completed', 'PrescriptionProvided']
        }

        seen_patients = []
        for p_email in distinct_patient_emails:
            patient_info_response = users_table.get_item(Key={'email': p_email})
            p_info = patient_info_response.get('Item')
            if p_info and 'patient_profile' in p_info:
                seen_patients.append({
                    'email': p_info['email'], 'name': p_info['name'],
                    'gender': p_info['patient_profile'].get('gender', 'N/A')
                })
        seen_patients.sort(key=lambda x: x['name']) # Sort by patient name for display
        return render_template('doctor_patients.html', patient_list=seen_patients)
    except ClientError as e:
        print(f"DynamoDB Error in doctor_patients: {e}")
        flash('Database error while fetching patient list.', 'error')
        return redirect(url_for('doctor_dashboard'))

# --- WSGI Entry Point ---
# For production deployment (e.g., Gunicorn, uWSGI)
application = app

if __name__ == '__main__':
    if users_table and appointments_table and medical_records_table: 
        print("AWS DynamoDB and SNS are configured. Initial data population skipped.")
    else:
        print("AWS services not fully initialized. Skipping initial data population.")
    app.run(debug=True) # For local development

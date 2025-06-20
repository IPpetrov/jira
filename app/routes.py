import os 
from datetime import timedelta 
from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, session, current_app
)
from jira import JIRA, JIRAError
import traceback
from .filters import get_filter_by_id
from . import jira_service
from .config import FILTERS, USER_SELECT_GROUP

bp = Blueprint('main', __name__)

# --- Helper to get Jira Client ---
def get_jira_client():
    """Creates and returns an authenticated JIRA client from session data."""
    if 'jira_email' not in session:
        flash("Session expired or invalid. Please login again.", "warning")
        return None
    try:
        server = session['jira_server']
        email = session['jira_email']
        token = session['jira_token']
        jira_options = {'server': server}
        jira_client = JIRA(options=jira_options, basic_auth=(email, token), max_retries=1)
        jira_client.myself() 
        return jira_client
    except JIRAError as e:
        current_app.logger.error(f"Failed to create/authenticate JIRA client: {e.status_code} - {e.text}")
        flash(f"Failed to connect to Jira: {e.text} (Status: {e.status_code}). Please check credentials.", "error")
        return None
    except Exception as e:
        current_app.logger.error(f"Unexpected error creating JIRA client: {e}")
        flash(f"An unexpected error occurred connecting to Jira: {e}", "error")
        return None

@bp.route('/')
def login():
    if 'jira_email' in session:
        jira_client = get_jira_client()
        if jira_client:
            return redirect(url_for('main.select_filter'))
        else:
            session.clear()
            flash("Your Jira session might have expired. Please login again.", "warning")
    return render_template('login.html')

@bp.route('/authenticate', methods=['POST'])
def authenticate():
    server = os.getenv("JIRA_SERVER")
    email = request.form.get('jira_email')
    token = request.form.get('jira_api_token')

    if not server or not email or not token:
        flash("Jira Server URL (in .env), Email, and API Token are required.", "error")
        return redirect(url_for('main.login'))

    try:
        jira_options = {'server': server}
        current_app.logger.info(f"Attempting to authenticate {email} on {server}")
        jira = JIRA(options=jira_options, basic_auth=(email, token), max_retries=1)
        user = jira.myself()
        current_app.logger.info(f"Authentication successful for user: {user.get('displayName', email)}")

        session.clear()
        session['jira_server'] = server
        session['jira_email'] = email
        session['jira_token'] = token
        session['user_display_name'] = user.get('displayName', email)
        session.permanent = True

        flash(f"Login successful as {session['user_display_name']}!", "success")

        return redirect(url_for('main.select_filter'))

    except JIRAError as e:
        current_app.logger.error(f"Jira Authentication Error: {e.status_code} - {e.text}")
        error_message = f"Login failed. Invalid credentials or Jira connection issue. Status: {e.status_code}."
        flash(error_message, "error")
        return redirect(url_for('main.login'))
    except Exception as e:
        current_app.logger.error(f"Generic Authentication Error: {e}")
        flash(f"An unexpected error occurred during login: {e}", "error")
        return redirect(url_for('main.login'))


@bp.route('/logout') 
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for('main.login'))


@bp.route('/select_filter')
def select_filter():
    if 'jira_email' not in session:
        flash("Please login first.", "warning")
        return redirect(url_for('main.login'))

    return render_template('select_filter.html',
                           filters=FILTERS,
                           username=session.get('user_display_name', 'User'))


@bp.route('/configure_filter/<filter_id>', methods=['GET', 'POST'])
def configure_filter(filter_id):
    if 'jira_email' not in session:
        flash("Please login first.", "warning")
        return redirect(url_for('main.login'))

    if filter_id not in FILTERS:
        flash(f"Filter '{filter_id}' does not exist.", "error")
        return redirect(url_for('main.select_filter'))

    selected_filter = FILTERS[filter_id]
    
    if request.method == 'POST':
        params_to_pass = {'filter_id': filter_id}
        for param_config in selected_filter.get('configurable_params', []):
            param_id = param_config['id']
            form_value = request.form.get(param_id)
            if form_value is not None:
                params_to_pass[param_id] = form_value

        return redirect(url_for('main.run_filter', **params_to_pass))

    is_configurable = any(p.get('type') != 'hidden' for p in selected_filter.get('configurable_params', []))
    if not is_configurable and request.method == 'GET':
        flash(f"Filter '{selected_filter['name']}' is not configurable. Running with defaults.", "info")
        return redirect(url_for('main.run_filter', filter_id=filter_id))

    # --- GET Request Logic ---
    jira_client = get_jira_client()
    if not jira_client:
        return redirect(url_for('main.login'))

    user_list_for_select = []
    has_user_select = any(p.get('type') == 'user_select' for p in selected_filter.get('configurable_params', []))
    
    if has_user_select:

        current_app.logger.info(f"Filter '{filter_id}' has a user select field. Fetching users from group '{USER_SELECT_GROUP}'.")
        try:
            user_list_for_select = jira_service.fetch_users_for_app_dropdown(jira_client, USER_SELECT_GROUP)
            current_app.logger.info(f"Successfully fetched {len(user_list_for_select)} users for the dropdown.")
        except JIRAError as e:
            flash(f"Jira Error fetching user list: {e.text}", "error")
            current_app.logger.error(f"Jira Error fetching group '{USER_SELECT_GROUP}': {e.text}")
        except Exception as e:
            flash("An unexpected error occurred while fetching the user list.", "error")
            tb_str = traceback.format_exc()
            current_app.logger.error(f"Unexpected error fetching group '{USER_SELECT_GROUP}': {e}\n{tb_str}")
            
    current_param_values = selected_filter.get('defaults', {}).copy()

    return render_template('configure_filter.html',
                           filter_id=filter_id,
                           filter_data=selected_filter,
                           current_values=current_param_values,
                           assignable_users=user_list_for_select,
                           username=session.get('user_display_name', 'User'))


@bp.route('/run_filter/<filter_id>')
def run_filter(filter_id):
    if 'jira_email' not in session:
        return redirect(url_for('main.login'))

    jira_client = get_jira_client()
    if not jira_client:
        return redirect(url_for('main.login'))

    user_params = request.args.to_dict()
    results, error_message, filter_instance = [], None, None

    try:
        filter_instance = get_filter_by_id(filter_id, user_params, jira_client)
        current_app.logger.info(f"Generated JQL: {filter_instance.jql}")
        issues = filter_instance.execute_search()
        results = filter_instance.process_results(issues)
        
    except Exception as e:
        error_message = f"An unexpected error occurred: {e}"
        tb_str = traceback.format_exc()
        current_app.logger.error(f"Error running filter '{filter_id}': {e}\n{tb_str}")
        
    return render_template('results.html',
                           results=results,
                           error=error_message,
                           filter_name=filter_instance.config['name'] if filter_instance else "Error",
                           result_title=filter_instance.config.get('result_title', '') if filter_instance else "Error",
                           filter_params_used=filter_instance.effective_params if filter_instance else {},
                           username=session.get('user_display_name', 'User'))
import os
from flask import Flask, render_template, request, redirect, url_for, flash, session
from jira import JIRA, JIRAError
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# --- JQL Helper ---
def sanitize_jql_list(input_list):
    """Takes a list of strings and formats them for a JQL 'in' clause."""
    if not input_list:
        return ""
    sanitized_items = [f'"{str(item).strip()}"' for item in input_list if str(item).strip()]
    if not sanitized_items:
        return ""
    return ", ".join(sanitized_items)

# --- Get users from a particular group ---
def get_users_from_group(jira, group_name):
    """
    Fetches members of a specific Jira group using direct REST API calls
    and returns them formatted for a Select2 dropdown.


    Returns:
        list: A list of dictionaries [{'id': accountId, 'text': displayName}]
              sorted alphabetically by display name, or an empty list on error.
    """
    formatted_users = []
    start_at = 0
    max_results = 50

    server_url = jira._options['server'].rstrip('/')
    api_endpoint = f"{server_url}/rest/api/2/group/member"

    print(f"Fetching members for group '{group_name}' from {api_endpoint}")

    while True:
        params = {
            "groupname": group_name,
            "startAt": start_at,
            "maxResults": max_results,
            "includeInactiveUsers": "false" 
        }
        try:
            response = jira._session.get(api_endpoint, params=params)
            response.raise_for_status()
            data = response.json()

            values = data.get('values', [])
            if not values and start_at == 0:
                 print(f"No members found in group '{group_name}' or group might not exist/be accessible.")
                 break

            for user_data in values:
                account_id = user_data.get('accountId')
                display_name = user_data.get('displayName')

                if account_id and display_name:
                    formatted_users.append({
                        'id': account_id,
                        'text': display_name
                    })
                else:
                    print(f"Warning: Skipping user entry with missing accountId or displayName: {user_data}")

            if data.get('isLast', False): 
                 break

            start_at += len(values) 

        except requests.exceptions.HTTPError as http_err:
            print(f"HTTP error fetching group members for '{group_name}': {http_err.response.status_code} - {http_err.response.text}")
            if http_err.response.status_code == 403:
                 flash(f"Permission Denied: Cannot access members for group '{group_name}'.", "error")
            elif http_err.response.status_code == 404:
                 flash(f"Group '{group_name}' not found.", "warning")
            else:
                 flash(f"Error fetching members for group '{group_name}': {http_err.response.status_code}", "error")
            return [] 
        except Exception as e:
            print(f"Unexpected error fetching group members for '{group_name}': {e}")
            import traceback
            traceback.print_exc()
            flash(f"An unexpected error occurred while fetching members for group '{group_name}'.", "error")
            return [] 

    formatted_users.sort(key=lambda x: x.get('text', '').lower())
    print(f"Successfully fetched and formatted {len(formatted_users)} members for group '{group_name}'.")
    return formatted_users

# --- Jira Issue Processing Helpers ---
def _extract_basic_issue_data(issue, server_url):
    """Extracts common fields from a Jira issue."""
    issue_type_name = "Unknown Type"
    status_name = "Unknown Status"
    if hasattr(issue.fields, 'issuetype') and hasattr(issue.fields.issuetype, 'name'):
        issue_type_name = issue.fields.issuetype.name
    if hasattr(issue.fields, 'status') and hasattr(issue.fields.status, 'name'):
        status_name = issue.fields.status.name

    return {
        'key': issue.key,
        'summary': issue.fields.summary,
        'issuetype': issue_type_name,
        'status': status_name,
        'url': f"{server_url}/browse/{issue.key}"
    }

# --- Filter Processors ---
def process_ready_tasks(jira, issues, config, server_url):
    """
    Processes issues for the 'Ready Tasks' filter.
    Uses 'resolved_category' and 'blocking_link_type' from the config dict.
    """
    ready_main_tasks_data = []
    resolved_category = config.get('resolved_category', 'Done') 
    blocking_link_type = config.get('blocking_link_type', 'Blocks') 

    for main_task in issues:
        basic_data = _extract_basic_issue_data(main_task, server_url)
        is_ready = False
        reason_parts = []
        has_blockers = False
        has_resolved_blockers = True 

        if hasattr(main_task.fields, 'issuelinks') and main_task.fields.issuelinks:
            for link in main_task.fields.issuelinks:
                if hasattr(link, 'type') and link.type.name == blocking_link_type:
                    if hasattr(link, 'inwardIssue'):
                        has_blockers = True
                        blocker = link.inwardIssue
                        blocker_status_category = "Unknown"
                        try:
                            blocker_status_category = blocker.fields.status.statusCategory.name
                        except AttributeError:
                            print(f"Warning: Could not determine status category for blocker {blocker.key}")

                        if blocker_status_category != resolved_category:
                            has_resolved_blockers = False
                            reason_parts.append(f"blocked by {blocker.key} (status: {blocker_status_category})")
                            break

        if not has_blockers:
            is_ready = True
            reason_parts.append("No blockers defined by link type '{blocking_link_type}'")
        elif has_resolved_blockers:
            is_ready = True
            reason_parts.append(f"All '{blocking_link_type}' blockers are resolved (in '{resolved_category}' category)")

        if is_ready:
            basic_data['reason'] = ", ".join(reason_parts)
            ready_main_tasks_data.append(basic_data)

    return ready_main_tasks_data


def process_parent_tasks_with_resolved_children(jira, issues, config, server_url):
    """
    Filters issues to find parents where all direct sub-tasks are resolved.
    Uses 'resolved_category' from the config dict.
    """
    parent_tasks_data = []
    resolved_category = config.get('resolved_category', 'Done')

    for issue in issues:
        if not hasattr(issue.fields, 'subtasks') or not issue.fields.subtasks:
            continue

        all_children_resolved = True
        subtask_details = []

        for subtask_ref in issue.fields.subtasks:
            subtask_status_category = "Unknown Status"
            try:
                if hasattr(subtask_ref, 'fields') and hasattr(subtask_ref.fields, 'status'):
                     subtask_status_category = subtask_ref.fields.status.statusCategory.name
                else:
                    print(f"Fetching details for subtask {subtask_ref.key} (parent {issue.key})")
                    subtask = jira.issue(subtask_ref.key, fields="status")
                    subtask_status_category = subtask.fields.status.statusCategory.name

            except AttributeError as e:
                 print(f"Error accessing status for subtask {subtask_ref.key} (parent {issue.key}): {e}")
                 all_children_resolved = False
            except JIRAError as e:
                 print(f"Jira Error fetching subtask {subtask_ref.key}: {e}")
                 all_children_resolved = False 

            subtask_details.append(f"{subtask_ref.key}: {subtask_status_category}")

            if subtask_status_category != resolved_category:
                all_children_resolved = False
                break

        if all_children_resolved:
            basic_data = _extract_basic_issue_data(issue, server_url)
            basic_data['reason'] = f"All sub-tasks resolved ({', '.join(subtask_details)})"
            parent_tasks_data.append(basic_data)

    return parent_tasks_data


def process_simple_list(jira, issues, config, server_url):
    """Generic processor for filters that just need a list of keys/summaries/status."""
    results_data = []
    for issue in issues:
        basic_data = _extract_basic_issue_data(issue, server_url)
        basic_data['reason'] = basic_data['status']
        results_data.append(basic_data)
    return results_data


# --- JQL Builder ---
def build_jql(filter_config, user_params):
    """
    Constructs the JQL query based on filter definition and user parameters.
    Handles 'Any Assignee' option.
    Returns:
        tuple: (jql_string, effective_params)
    """
    defaults = filter_config.get('defaults', {})
    effective_params = defaults.copy()
    effective_params.update(user_params) 

    list_params = ['projects', 'exclude_types', 'include_types']
    for key in list_params:
        if key in effective_params:
            if isinstance(effective_params[key], str):
                effective_params[key] = [p.strip() for p in effective_params[key].split(',') if p.strip()]
            elif not isinstance(effective_params[key], list):
                effective_params[key] = []
        else:
            effective_params[key] = []

    if 'assignee' not in effective_params or not effective_params['assignee']:
         effective_params['assignee'] = 'currentUser()'

    adjust_assignee_quoting(effective_params)

    jql_parts = []
    assignee_jql_part = None 

    selected_assignee = effective_params.get('assignee')
    if selected_assignee and selected_assignee != '__any__':
        assignee_jql_part = f"assignee = {selected_assignee}"


    base_jql_template = filter_config.get('base_jql_template', '')
    if base_jql_template:
        try:
            formatted_base = base_jql_template.format(**effective_params)
            if formatted_base:
                 jql_parts.append(f"({formatted_base})")
        except KeyError as e:
             print(f"Warning: JQL template '{base_jql_template}' missing key {e}. Params: {effective_params}")
        except Exception as e:
             print(f"Warning: Error formatting base JQL template '{base_jql_template}': {e}")


    if assignee_jql_part:
        jql_parts.append(assignee_jql_part)

    if effective_params.get('projects'):
        sanitized_projects = sanitize_jql_list(effective_params['projects'])
        if sanitized_projects:
            jql_parts.append(f"project in ({sanitized_projects})")
    if effective_params.get('include_types'):
        sanitized_includes = sanitize_jql_list(effective_params['include_types'])
        if sanitized_includes:
            jql_parts.append(f"issuetype in ({sanitized_includes})")
    if effective_params.get('exclude_types'):
        sanitized_excludes = sanitize_jql_list(effective_params['exclude_types'])
        if sanitized_excludes:
            jql_parts.append(f"issuetype not in ({sanitized_excludes})")


    jql_string = " AND ".join(filter(None, jql_parts)) 

    order_by = filter_config.get('order_by', 'ORDER BY updated DESC')
    if jql_string and order_by:
        jql_string += f" {order_by}"
    elif not jql_string:
         print("Warning: JQL string is empty after processing.")

    return jql_string, effective_params

def adjust_assignee_quoting(params_dict):
    """
    Adjusts the 'assignee' value in the parameters dictionary for JQL compatibility.

    If the 'assignee' value exists, is a string, is not 'currentUser()',
    is not '__any__', and is not already enclosed in double quotes,
    it wraps the value in double quotes.

    Returns:
        dict: The dictionary with the assignee value potentially adjusted.
              Note: The dictionary is modified in-place.
    """
    assignee_key = 'assignee'

    if assignee_key in params_dict:
        assignee_value = params_dict[assignee_key]

        if isinstance(assignee_value, str):
            if (assignee_value != "currentUser()" and
                    assignee_value != "__any__" and
                    not (assignee_value.startswith('"') and assignee_value.endswith('"'))):
                print(f"Adjusting assignee: Wrapping '{assignee_value}' in quotes for JQL.")
                params_dict[assignee_key] = f'"{assignee_value}"'

    return params_dict

# --- JQL Builder ---
def build_jql(filter_config, user_params):
    """
    Constructs the JQL query based on filter definition and user parameters.
    Handles 'Any Assignee' option.
    Returns:
        tuple: (jql_string, effective_params)
    """
    defaults = filter_config.get('defaults', {})
    effective_params = defaults.copy()
    effective_params.update(user_params) 

    list_params = ['projects', 'exclude_types', 'include_types']
    for key in list_params:
        if key in effective_params:
            if isinstance(effective_params[key], str):
                effective_params[key] = [p.strip() for p in effective_params[key].split(',') if p.strip()]
            elif not isinstance(effective_params[key], list):
                effective_params[key] = []
        else:
            effective_params[key] = []

    if 'assignee' not in effective_params or not effective_params['assignee']:
         effective_params['assignee'] = 'currentUser()'

    adjust_assignee_quoting(effective_params)

    # --- JQL Construction ---
    jql_parts = []
    assignee_jql_part = None 

    selected_assignee = effective_params.get('assignee')
    if selected_assignee and selected_assignee != '__any__':
        assignee_jql_part = f"assignee = {selected_assignee}"


    base_jql_template = filter_config.get('base_jql_template', '')
    if base_jql_template:
        try:
            formatted_base = base_jql_template.format(**effective_params)
            if formatted_base:
                 jql_parts.append(f"({formatted_base})")
        except KeyError as e:
             print(f"Warning: JQL template '{base_jql_template}' missing key {e}. Params: {effective_params}")
        except Exception as e:
             print(f"Warning: Error formatting base JQL template '{base_jql_template}': {e}")


    if assignee_jql_part:
        jql_parts.append(assignee_jql_part)


    if effective_params.get('projects'):
        sanitized_projects = sanitize_jql_list(effective_params['projects'])
        if sanitized_projects:
            jql_parts.append(f"project in ({sanitized_projects})")
    if effective_params.get('include_types'):
        sanitized_includes = sanitize_jql_list(effective_params['include_types'])
        if sanitized_includes:
            jql_parts.append(f"issuetype in ({sanitized_includes})")
    if effective_params.get('exclude_types'):
        sanitized_excludes = sanitize_jql_list(effective_params['exclude_types'])
        if sanitized_excludes:
            jql_parts.append(f"issuetype not in ({sanitized_excludes})")


    jql_string = " AND ".join(filter(None, jql_parts)) 

    order_by = filter_config.get('order_by', 'ORDER BY updated DESC')
    if jql_string and order_by:
        jql_string += f" {order_by}"
    elif not jql_string:
         print("Warning: JQL string is empty after processing.")

    return jql_string, effective_params

# --- Filter Definitions ---
FILTERS = {
    "ready_tasks": {
        "name": "Ready Tasks (Blockers Resolved)",
        "description": "Tasks assigned to you where any 'Blocks' links point to resolved issues, or the task has no 'Blocks' links. Open tasks only.",
        "configurable_params": [ 
             {
                "id": "projects",
                "label": "Limit to Projects (comma-separated)",
                "type": "text",
                "help_text": "Enter Jira project keys (e.g., STM, PROJ). Leave blank for defaults or to include all assigned projects."
             },
             {
                 "id": "include_types",
                 "label": "Include Parent Issue Types (comma-separated)",
                 "type": "text",
                 "help_text": "Only show parents of these types (e.g., Story, Task). Leave blank for all types not excluded."
             },
             {
                 "id": "exclude_types",
                 "label": "Exclude Issue Types (comma-separated)",
                 "type": "text",
                 "help_text": "Issue types to ignore (e.g., Sub-task, Story). Default excludes Epics."
             },
             {
                 "id": "assignee",
                 "label": "Assignee",
                 "type": "user_select",
                 "help_text": "Select the assignee. Defaults to the current user."
             },
        ],
        "defaults": { 
            "projects": ["STM", "DEL"], #
            "assignee": "currentUser()", 
            "exclude_types": ["Epic"],   
            "resolved_category": "Done", 
            "blocking_link_type": "Blocks" 
        },
        "base_jql_template": "statusCategory != '{resolved_category}'", 
        "fields": "key,summary,issuelinks,status,issuetype", 
        "processor": process_ready_tasks, 
        "result_title": "My Tasks Ready for Work", 
        "order_by": "ORDER BY updated DESC"
    },
    "parents_resolved_children": {
        "name": "Parent Tasks (Sub-tasks Done)",
        "description": "Shows your open parent tasks (e.g., Stories, Tasks) where all direct sub-tasks are in the 'Done' status category.",
         "configurable_params": [
             {
                 "id": "projects",
                 "label": "Parent Projects (comma-separated)",
                 "type": "text",
                 "help_text": "Limit parent tasks to these projects. Leave blank for defaults."
              },
             {
                 "id": "include_types",
                 "label": "Include Parent Issue Types (comma-separated)",
                 "type": "text",
                 "help_text": "Only show parents of these types (e.g., Story, Task). Leave blank for all types not excluded."
             },
             {
                 "id": "exclude_types",
                 "label": "Exclude Parent Issue Types (comma-separated)",
                 "type": "text",
                 "help_text": "Ignore parents of these types. Default excludes Epics and Sub-tasks."
             },
        ],
        "defaults": {
            "projects": ["STM", "DEL"],
            "assignee": "currentUser()",
            "include_types": [], 
            "exclude_types": ["Epic", "Sub-task"], 
            "resolved_category": "Done" 
        },
        "base_jql_template": "assignee = {assignee} AND statusCategory != '{resolved_category}'",
        "fields": "key,summary,issuetype,status,subtasks",
        "processor": process_parent_tasks_with_resolved_children,
        "result_title": "My Tasks with All Sub-tasks Resolved",
        "order_by": "ORDER BY updated DESC"
    }
}

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
        jira = JIRA(options=jira_options, basic_auth=(email, token), max_retries=1) 
        jira.myself()
        return jira
    except JIRAError as e:
        print(f"Failed to create/authenticate JIRA client: {e.status_code} - {e.text}")
        flash(f"Failed to connect to Jira: {e.text} (Status: {e.status_code}). Please check connection or credentials.", "error")
        return None
    except Exception as e:
        print(f"Unexpected error creating JIRA client: {e}")
        flash(f"An unexpected error occurred connecting to Jira: {e}", "error")
        return None


# --- Flask Routes ---
@app.route('/')
def login():
    if 'jira_email' in session:
        jira = get_jira_client()
        if jira:
            return redirect(url_for('select_filter'))
        else:
            session.clear()
            flash("Your Jira session might have expired. Please login again.", "warning")
    return render_template('login.html')


@app.route('/authenticate', methods=['POST'])
def authenticate():
    server = os.getenv("JIRA_SERVER")
    email = request.form.get('jira_email')
    token = request.form.get('jira_api_token')

    if not server or not email or not token:
        flash("Jira Server URL (in .env), Email, and API Token are required.", "error")
        return redirect(url_for('login'))

    try:
        jira_options = {'server': server}
        print(f"Attempting to authenticate {email} on {server}")
        jira = JIRA(options=jira_options, basic_auth=(email, token), max_retries=1)
        user = jira.myself()
        print(f"Authentication successful for user: {user.get('displayName', email)}")

        session.clear()
        session['jira_server'] = server
        session['jira_email'] = email
        session['jira_token'] = token
        session['user_display_name'] = user.get('displayName', email)
        session.permanent = True
        app.permanent_session_lifetime = timedelta(days=7)

        flash(f"Login successful as {session['user_display_name']}!", "success")
        return redirect(url_for('select_filter'))

    except JIRAError as e:
        print(f"Jira Authentication Error: {e.status_code} - {e.text}")
        error_message = f"Login failed. Invalid credentials or Jira connection issue. Status: {e.status_code}. Check details and try again."
        flash(error_message, "error")
        return redirect(url_for('login'))
    except Exception as e:
        print(f"Generic Authentication Error: {e}")
        flash(f"An unexpected error occurred during login: {e}", "error")
        return redirect(url_for('login'))


@app.route('/logout')
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))


@app.route('/select_filter')
def select_filter():
    if 'jira_email' not in session:
        flash("Please login first.", "warning")
        return redirect(url_for('login'))

    return render_template('select_filter.html',
                           filters=FILTERS,
                           username=session.get('user_display_name', 'User'))


@app.route('/configure_filter/<filter_id>', methods=['GET', 'POST'])
def configure_filter(filter_id):
    if 'jira_email' not in session:
        flash("Please login first.", "warning")
        return redirect(url_for('login'))

    if filter_id not in FILTERS:
        flash(f"Filter '{filter_id}' does not exist.", "error")
        return redirect(url_for('select_filter'))

    selected_filter = FILTERS[filter_id]

    is_configurable = any(p.get('type') != 'hidden' for p in selected_filter.get('configurable_params', []))
    if not is_configurable:
        flash(f"Filter '{selected_filter['name']}' is not configurable. Running with defaults.", "info")
        return redirect(url_for('run_filter', filter_id=filter_id))

    if request.method == 'POST':
        params_to_pass = {'filter_id': filter_id}
        for param_config in selected_filter.get('configurable_params', []):
            param_id = param_config['id']
            form_value = request.form.get(param_id)
            if form_value is not None:
                 params_to_pass[param_id] = form_value
        return redirect(url_for('run_filter', **params_to_pass))

    jira = get_jira_client()
    user_list_for_select = []
    current_param_values = selected_filter.get('defaults', {}).copy()

    assignee_param_config = next((p for p in selected_filter.get('configurable_params', []) if p['id'] == 'assignee' and p['type'] == 'user_select'), None)

    if assignee_param_config and jira:
        target_group = "PMO"
        print(f"Assignee is user_select, attempting to fetch users from group: {target_group}")
        user_list_for_select = get_users_from_group(jira, target_group)

    return render_template('configure_filter.html',
                           filter_id=filter_id,
                           filter_data=selected_filter,
                           current_values=current_param_values, 
                           assignable_users=user_list_for_select, 
                           username=session.get('user_display_name', 'User'))


@app.route('/run_filter/<filter_id>')
def run_filter(filter_id):
    if 'jira_email' not in session:
        flash("Please login first.", "warning")
        return redirect(url_for('login'))

    if filter_id not in FILTERS:
        flash(f"Invalid filter ID: {filter_id}", "error")
        return redirect(url_for('select_filter'))

    selected_filter = FILTERS[filter_id]
    server_url = session['jira_server']

    jira = get_jira_client()
    if not jira:
        return redirect(url_for('login'))

    user_params = request.args.to_dict()
    user_params.pop('filter_id', None)

    # --- Build JQL ---
    try:
        jql, effective_params = build_jql(selected_filter, user_params)
        print(f"Running filter '{filter_id}' with JQL: {jql}")
        print(f"Effective parameters used: {effective_params}")
    except Exception as e:
         print(f"Error building JQL for filter '{filter_id}': {e}")
         flash(f"Error preparing filter '{selected_filter['name']}': {e}", "error")
         return redirect(url_for('select_filter')) 

    if not jql:
         flash(f"Could not generate a valid query for filter '{selected_filter['name']}'. Check filter configuration.", "error")
         return render_template('results.html',
                               error=f"Filter '{selected_filter['name']}' did not produce a valid JQL query.",
                               filter_name=selected_filter['name'],
                               result_title=f"Error running {selected_filter['name']}",
                               filter_params_used=effective_params,
                               username=session.get('user_display_name', 'User'))

    # --- Execute JQL and Process Results ---
    results = []
    error_message = None
    try:
        fields_to_request = selected_filter.get('fields', 'key,summary,status,issuetype')
        print(f"Requesting fields: {fields_to_request}")

        issues = jira.search_issues(jql, maxResults=50, fields=fields_to_request, expand='fields.status.statusCategory')
        print(f"Found {len(issues)} raw issues for filter '{filter_id}' (max 50 requested)")

        processor_func = selected_filter.get('processor', process_simple_list)
        processor_args = {
            'jira': jira,
            'issues': issues,
            'config': effective_params,
            'server_url': server_url
        }
        results = processor_func(**processor_args)
        print(f"Processed {len(results)} results for filter '{filter_id}'")

    except JIRAError as e:
        print(f"Jira Query Error for filter '{filter_id}': {e.status_code} - {e.text}")
        print(f"Failed JQL: {jql}") 
        error_message = f"Jira Error running filter '{selected_filter['name']}': Status {e.status_code}. Details: {e.text}. Query: {jql}"
    except Exception as e:
        print(f"Generic Processing Error for filter '{filter_id}': {e}")
        import traceback
        traceback.print_exc() 
        error_message = f"An unexpected error occurred running filter '{selected_filter['name']}': {e}"

    # --- Render Results ---
    return render_template('results.html',
                           results=results if not error_message else [], 
                           error=error_message,
                           filter_name=selected_filter['name'],
                           result_title=selected_filter.get('result_title', 'Jira Results'),
                           filter_params_used=effective_params, 
                           username=session.get('user_display_name', 'User'))

from datetime import timedelta

if __name__ == "__main__":
    app.permanent_session_lifetime = timedelta(days=7)
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
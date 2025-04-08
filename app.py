from flask import Flask, render_template, request, redirect, url_for, flash, session # Added flash and session
from jira import JIRA, JIRAError
from dotenv import load_dotenv
import os

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# --- Filter Definitions ---
# We'll define our filters here. Start with your current logic.
# We can add more later.
# Let's also abstract the post-processing logic.

# Defaults specific to the ready_tasks filter
READY_TASKS_DEFAULTS = {
    "projects": ["STM", "DEL"],
    "assignee": "currentUser()",
    "exclude_types": ["Epic"], # Epic is always the base exclusion
    "resolved_category": "Done",
    "blocking_link_type": "Blocks"
}

PARENT_TASK_DEFAULTS = {
    "projects": ["STM", "DEL"], # Or make configurable
    "assignee": "currentUser()",
    "parent_types": ["Story", "Task", "Bug"], # Types that can HAVE sub-tasks
    "resolved_category": "Done" # Status category name for "resolved"
}

# --- Helper for JQL Construction ---
def sanitize_jql_list(input_list):
    """Takes a list of strings and formats them for a JQL 'in' clause."""
    if not input_list:
        return "" # Return empty string if list is empty or None
    # Ensure items are strings, strip whitespace, quote them, and filter out empty strings after stripping
    sanitized_items = [f'"{str(item).strip()}"' for item in input_list if str(item).strip()]
    if not sanitized_items:
        return "" # Return empty string if all items were whitespace or empty
    return ", ".join(sanitized_items)

def process_linked_tasks_filter(jira ,issues, resolved_category, blocking_link_type, server_url):
    """
    Processes issues specifically for the 'Ready Tasks' filter logic.
    Now takes resolved_category and blocking_link_type as arguments.
    """
    ready_main_tasks_data = []

    for main_task in issues:
        main_task_key = main_task.key
        main_task_summary = main_task.fields.summary
        is_ready = False
        reason_parts = []
        has_blockers = False
        has_resolved_blockers = True # Assume true until proven false

        if hasattr(main_task.fields, 'issuelinks') and main_task.fields.issuelinks:
            for link in main_task.fields.issuelinks:
                if hasattr(link, 'type') and link.type.name == blocking_link_type:
                    if hasattr(link, 'inwardIssue'):
                        has_blockers = True
                        blocker_key = link.inwardIssue.key
                        blocker_status_category = None
                        if hasattr(link.inwardIssue, 'fields') and \
                           hasattr(link.inwardIssue.fields, 'status') and \
                           hasattr(link.inwardIssue.fields.status, 'statusCategory') and \
                           hasattr(link.inwardIssue.fields.status.statusCategory, 'name'):
                           blocker_status_category = link.inwardIssue.fields.status.statusCategory.name

                        if blocker_status_category != resolved_category:
                            has_resolved_blockers = False
                            reason_parts.append(f"blocked by {blocker_key} (status: {blocker_status_category or 'Unknown'})")
                            break

        if not has_blockers:
            is_ready = True
            reason_parts.append("No blockers")
        elif has_resolved_blockers:
            is_ready = True
            reason_parts.append("All blockers resolved")

        if is_ready:
            issue_type_name = "Unknown Type"
            if hasattr(main_task.fields, 'issuetype') and hasattr(main_task.fields.issuetype, 'name'):
                issue_type_name = main_task.fields.issuetype.name

            ready_main_tasks_data.append({
                'key': main_task_key,
                'summary': main_task_summary,
                'reason': ", ".join(reason_parts),
                'url': f"{server_url}/browse/{main_task_key}",
                'issuetype': issue_type_name
            })
    return ready_main_tasks_data

def process_parent_tasks_filter(jira, issues, resolved_category, server_url):
    """
    Filters issues to find parents where all direct sub-tasks are resolved.

    Args:
        jira: The authenticated JIRA client instance.
        issues: A list of candidate parent issues fetched via JQL.
        resolved_category (str): The name of the status category considered "Done" (e.g., "Done").
        server_url (str): The base URL of the Jira server for constructing links.

    Returns:
        list: A list of dictionaries, each representing a parent task whose sub-tasks are all resolved.
    """
    parent_tasks_with_resolved_children = []

    for issue in issues:
        # Check if the issue has subtasks field and it's populated
        if not hasattr(issue.fields, 'subtasks') or not issue.fields.subtasks:
            continue # Skip issues with no subtasks for this filter's logic

        all_children_resolved = True
        subtask_details = [] # To store info for the reason string

        # Iterate through the subtasks linked to the current issue
        for subtask in issue.fields.subtasks:
            subtask_status_category = "Unknown Status" # Default
            # Safely access the status category name
            try:
                if hasattr(subtask, 'fields') and \
                   hasattr(subtask.fields, 'status') and \
                   hasattr(subtask.fields.status, 'statusCategory') and \
                   hasattr(subtask.fields.status.statusCategory, 'name'):
                   subtask_status_category = subtask.fields.status.statusCategory.name
                else:
                    # Attempt to fetch the subtask fully if status is missing (less efficient, use if necessary)
                    # subtask_full = jira.issue(subtask.key, fields="status")
                    # subtask_status_category = subtask_full.fields.status.statusCategory.name
                    print(f"Warning: Could not determine status category directly for subtask {subtask.key}")

            except AttributeError as e:
                 print(f"Error accessing status for subtask {subtask.key}: {e}")
                 # Decide how to handle - treat as unresolved?
                 all_children_resolved = False # Safer to assume not resolved if status unknown
            except JIRAError as e:
                 print(f"Jira Error fetching subtask {subtask.key}: {e}")
                 all_children_resolved = False # Assume not resolved if fetch fails


            subtask_details.append(f"{subtask.key}: {subtask_status_category}")

            # If any subtask is not in the resolved category, the parent doesn't qualify
            if subtask_status_category != resolved_category:
                all_children_resolved = False
                break # No need to check other subtasks for this parent

        # If after checking all subtasks, they were all resolved...
        if all_children_resolved:
            # Safely get parent issue type name
            issue_type_name = "Unknown Type"
            if hasattr(issue.fields, 'issuetype') and hasattr(issue.fields.issuetype, 'name'):
                issue_type_name = issue.fields.issuetype.name

            parent_tasks_with_resolved_children.append({
                'key': issue.key,
                'summary': issue.fields.summary,
                'issuetype': issue_type_name,
                'url': f"{server_url}/browse/{issue.key}",
                'reason': f"All sub-tasks resolved ({', '.join(subtask_details)})"
            })

    return parent_tasks_with_resolved_children

# (Keep process_simple_list as before, ensure it takes server_url)
def process_simple_list(jira, issues, server_url):
    """Generic processor for filters that just need a list of keys/summaries."""
    # ... (ensure server_url is used for task.url) ...
    results_data = []
    for issue in issues:
        issue_type_name = "Unknown Type"
        if hasattr(issue.fields, 'issuetype') and hasattr(issue.fields.issuetype, 'name'):
            issue_type_name = issue.fields.issuetype.name

        results_data.append({
            'key': issue.key,
            'summary': issue.fields.summary,
            'reason': issue.fields.status.name if hasattr(issue.fields, 'status') else 'N/A',
            'url': f"{server_url}/browse/{issue.key}",
            'issuetype': issue_type_name
        })
    return results_data


FILTERS = {
    "ready_tasks": {
        "name": "Open tasks with linked tasks",
        "description": "Tasks assigned to you where any 'Blocks' links point to either all resolved issues or there's no links. Configure projects and excluded types.",
        "configurable": True, # Mark as configurable
        "configurable_params": ["projects", "exclude_types"], # List parameters users can set
        "defaults": READY_TASKS_DEFAULTS, # Refer to the defaults dictionary
        # Base JQL parts - we will construct the full JQL dynamically
        "base_jql": "assignee = {assignee} AND statusCategory != '{resolved_category}'",
        "fields": "key,summary,issuelinks,status,issuetype",
        "processor": process_linked_tasks_filter,
        "processor_args": ["resolved_category", "blocking_link_type"], # Args needed by the processor beyond jira, issues, server_url
        "result_title": "My Open Tasks With Linked Issues"
    },
     "parents_resolved_children": {
        "name": "My Tasks with All Sub-tasks Resolved",
        "description": "Shows your open tickets, where all direct sub-tasks are in the 'Done' status category.",
        "configurable": False, # Can be set to True later to configure projects/parent_types
        "defaults": PARENT_TASK_DEFAULTS,
        # JQL to find candidate parents: Assigned to user, in specified projects,
        # of the potential parent types, and NOT already resolved themselves.
        "jql": (
            f"project in ({sanitize_jql_list(PARENT_TASK_DEFAULTS['projects'])}) "
            f"AND assignee = {PARENT_TASK_DEFAULTS['assignee']} "
            f"AND issuetype in ({sanitize_jql_list(PARENT_TASK_DEFAULTS['parent_types'])}) "
            f"AND statusCategory != '{PARENT_TASK_DEFAULTS['resolved_category']}' "
            f"ORDER BY updated DESC"
        ),
        # Fields needed for JQL AND for the processor function (key, summary, issuetype, subtasks.fields.status.statusCategory)
        # NOTE: Requesting subtask fields directly in parent search is limited.
        # We fetch the basic subtask list here, processor checks status.
        "fields": "key,summary,issuetype,status,subtasks",
        "processor": process_parent_tasks_filter,
        # Specify arguments the processor needs from defaults/config
        "processor_args": ["resolved_category"],
        "result_title": "Tasks with Resolved Sub-tasks"
    }
}

@app.route('/')
def login():
    # If already logged in (session exists), maybe redirect to filter selection?
    if 'jira_email' in session:
        return redirect(url_for('select_filter'))
    return render_template('login.html')

@app.route('/authenticate', methods=['POST'])
def authenticate():
    server = os.getenv("JIRA_SERVER")
    email = request.form.get('jira_email')
    token = request.form.get('jira_api_token')

    if not server or not email or not token:
        flash("Jira Server URL, Email, and API Token are required.", "error")
        return redirect(url_for('login'))

    try:
        # Try connecting to Jira to validate credentials
        jira_options = {'server': server}
        print(f"Attempting to authenticate {email} on {server}") # Debugging
        jira = JIRA(options=jira_options, basic_auth=(email, token))
        # Test connection by getting user info (optional but good practice)
        user = jira.myself()
        print(f"Authentication successful for user: {user.get('displayName', email)}") # Debugging

        # Store credentials SECURELY in session
        session['jira_server'] = server
        session['jira_email'] = email
        session['jira_token'] = token
        session['user_display_name'] = user.get('displayName', email) # Store display name

        flash(f"Login successful as {session['user_display_name']}!", "success")
        # Redirect to the new filter selection page
        return redirect(url_for('select_filter'))

    except JIRAError as e:
        print(f"Jira Authentication Error: {e.status_code} - {e.text}") # Debugging
        error_message = f"Login failed. Invalid credentials or Jira connection issue. Status: {e.status_code}. Check details and try again."
        # You might want to inspect e.text for more specific Jira error messages if needed
        flash(error_message, "error")
        return redirect(url_for('login'))
    except Exception as e:
        # Catch other potential errors (network issues, etc.)
        print(f"Generic Authentication Error: {e}") # Debugging
        flash(f"An unexpected error occurred during login: {e}", "error")
        return redirect(url_for('login'))


@app.route('/logout')
def logout():
    # Clear the session
    session.pop('jira_server', None)
    session.pop('jira_email', None)
    session.pop('jira_token', None)
    session.pop('user_display_name', None)
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))

@app.route('/select_filter')
def select_filter():
    if 'jira_email' not in session:
        flash("Please login first.", "warning")
        return redirect(url_for('login'))
    return render_template('select_filter.html', filters=FILTERS, username=session.get('user_display_name', 'User'))

# --- NEW Route for Configuration Form ---
@app.route('/configure_filter/<filter_id>', methods=['GET', 'POST'])
def configure_filter(filter_id):
    if 'jira_email' not in session:
        flash("Please login first.", "warning")
        return redirect(url_for('login'))

    if filter_id not in FILTERS or not FILTERS[filter_id].get('configurable'):
        flash(f"Filter '{filter_id}' is not configurable or does not exist.", "error")
        return redirect(url_for('select_filter'))

    selected_filter = FILTERS[filter_id]
    defaults = selected_filter.get('defaults', {})

    if request.method == 'POST':
        # User submitted the form, gather parameters and redirect to run_filter
        params_to_pass = {'filter_id': filter_id}
        if "projects" in selected_filter.get('configurable_params', []):
            projects_str = request.form.get('projects', ", ".join(defaults.get('projects', [])))
            params_to_pass['projects'] = projects_str
        if "exclude_types" in selected_filter.get('configurable_params', []):
            exclude_types_str = request.form.get('exclude_types', ", ".join(defaults.get('exclude_types', [])))
             # Ensure Epic is always included in the exclusion list submitted
            user_excludes = [t.strip() for t in exclude_types_str.split(',') if t.strip()]
            params_to_pass['exclude_types'] = ",".join(user_excludes)

        # Add other potential future parameters here...

        # Redirect to run_filter with parameters in query string
        return redirect(url_for('run_filter', **params_to_pass))

    # --- GET Request: Display the configuration form ---
    # Prepare default values for the form
    form_defaults = {
        'projects': ", ".join(defaults.get('projects', [])),
        'exclude_types': ", ".join(defaults.get('exclude_types', []))
        # Add others if needed
    }

    return render_template('configure_filter.html',
                           filter_id=filter_id,
                           filter_data=selected_filter,
                           form_defaults=form_defaults,
                           username=session.get('user_display_name', 'User'))


@app.route('/run_filter/<filter_id>') # filter_id from URL path
def run_filter(filter_id):
    if 'jira_email' not in session:
        flash("Please login first.", "warning")
        return redirect(url_for('login'))

    if filter_id not in FILTERS:
        flash(f"Invalid filter ID: {filter_id}", "error")
        return redirect(url_for('select_filter'))

    selected_filter = FILTERS[filter_id]
    server = session['jira_server']
    email = session['jira_email']
    token = session['jira_token']
    defaults = selected_filter.get('defaults', {})

    # --- JQL Construction ---
    jql = ""
    filter_params_used = {} # To store parameters used for this run

    if selected_filter.get('configurable'):
        # Build JQL dynamically using base_jql and query parameters
        base_jql = selected_filter.get('base_jql', '')

        # Get parameters from request.args or use defaults from filter definition
        assignee = defaults.get('assignee', 'currentUser()') # Assignee usually fixed
        resolved_category = defaults.get('resolved_category', 'Done')

        projects_str = request.args.get('projects', ",".join(defaults.get('projects', [])))
        project_list = [p.strip() for p in projects_str.split(',') if p.strip()]

        exclude_types_str = request.args.get('exclude_types', ",".join(defaults.get('exclude_types', [])))
         # Ensure 'Epic' is always excluded if this param is configurable
        exclude_list = [t.strip() for t in exclude_types_str.split(',') if t.strip()]

        # Store used parameters
        filter_params_used['Assignee'] = assignee
        filter_params_used['Projects'] = ", ".join(project_list) if project_list else "None Specified"
        filter_params_used['Excluded Types'] = ", ".join(exclude_list) if exclude_list else "None"
        filter_params_used['Resolved Category'] = resolved_category

        # Format base JQL with non-list parameters
        jql = base_jql.format(assignee=assignee, resolved_category=resolved_category)

        # Append list parameters conditionally
        if project_list:
            jql += f" AND project in ({sanitize_jql_list(project_list)})"
        if exclude_list:
            jql += f" AND issuetype not in ({sanitize_jql_list(exclude_list)})"

        # Add ORDER BY if needed
        jql += " ORDER BY updated DESC" # Example default sorting

    else:
        # Use static JQL for non-configurable filters
        jql = selected_filter.get('jql', '')
        filter_params_used['Info'] = "This filter uses a predefined query."


    # --- Execute Query ---
    fields = selected_filter.get('fields', 'key,summary,status')
    processor_func = selected_filter.get('processor', process_simple_list)
    result_title = selected_filter.get('result_title', 'Jira Results')

    try:
        jira_options = {'server': server}
        jira = JIRA(options=jira_options, basic_auth=(email, token))

        print(f"Running filter '{filter_id}' with JQL: {jql}")
        issues = jira.search_issues(jql, maxResults=False, fields=fields)
        print(f"Found {len(issues)} raw issues for filter '{filter_id}'")

        # Prepare arguments for the processor function
        processor_kwargs = {'jira': jira, 'issues': issues, 'server_url': server}
        # Add specific args required by the processor (like resolved_category)
        if selected_filter.get('processor_args'):
            for arg_name in selected_filter['processor_args']:
                # Get value from defaults used in JQL construction for consistency
                arg_value = defaults.get(arg_name)
                if arg_value is not None: # Check if the key exists and has a value in defaults
                     processor_kwargs[arg_name] = arg_value # Add it to the arguments dict
                else:
                    print(f"Warning: Processor argument '{arg_name}' not found in defaults for filter '{filter_id}'")

        processed_results = processor_func(**processor_kwargs)
        print(f"Processed {len(processed_results)} results for filter '{filter_id}'")

        return render_template('results.html',
                               results=processed_results,
                               filter_name=selected_filter['name'],
                               # filter_description=selected_filter.get('description', ''), # Can remove if redundant
                               result_title=result_title,
                               filter_params_used=filter_params_used, # Pass used params
                               username=session.get('user_display_name', 'User')
                               )

    except JIRAError as e:
        # (Error handling as before, but pass filter_params_used if available)
        print(f"Jira Query Error for filter '{filter_id}': {e.status_code} - {e.text}")
        error_message = f"Jira Error running filter '{selected_filter['name']}': Status {e.status_code}. Details: {e.text}"
        return render_template('results.html',
                               error=error_message,
                               filter_name=selected_filter['name'],
                               # filter_description=selected_filter.get('description', ''),
                               result_title=f"Error running {selected_filter['name']}",
                               filter_params_used=filter_params_used, # Pass params even on error
                               username=session.get('user_display_name', 'User')
                               )
    except Exception as e:
        # (Generic error handling as before)
        print(f"Generic Query Error for filter '{filter_id}': {e}") # Debugging
        error_message = f"An unexpected error occurred running filter '{selected_filter['name']}': {e}"
        return render_template('results.html',
                               error=error_message,
                               filter_name=selected_filter['name'],
                               result_title=f"Error running {selected_filter['name']}",
                               filter_params_used=filter_params_used,
                               username=session.get('user_display_name', 'User')
                               )

if __name__ == '__main__':
    app.run(debug=True) # Enable debug mode for local development
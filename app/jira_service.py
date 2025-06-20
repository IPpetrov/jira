import requests
from jira import JIRAError
from .utils import sanitize_jql_list


def fetch_users_for_app_dropdown(jira_client, group_name):
    """
    Fetches members of a specific Jira group.
    """
    server_url = jira_client._options['server'].rstrip('/')
    api_endpoint = f"{server_url}/rest/api/2/group/member"
    formatted_users = []
    start_at = 0
    while True:
        params = {"groupname": group_name, "startAt": start_at, "maxResults": 50, "includeInactiveUsers": "false"}
        # This is the line that crashes
        response = jira_client._session.get(api_endpoint, params=params)
        response.raise_for_status()
        data = response.json()
        values = data.get('values', [])
        for user_data in values:
            if user_data.get('accountId') and user_data.get('displayName'):
                formatted_users.append({'id': user_data['accountId'], 'text': user_data['displayName']})
        if data.get('isLast', False):
            break
        start_at += len(values)
    formatted_users.sort(key=lambda x: x.get('text', '').lower())
    return formatted_users

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
def process_ready_tasks(jira_client, issues, config, server_url):
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

        if has_resolved_blockers:
            is_ready = True
            reason_parts.append(f"All '{blocking_link_type}' blockers are resolved (in '{resolved_category}' category)")

        if is_ready:
            basic_data['reason'] = ", ".join(reason_parts)
            ready_main_tasks_data.append(basic_data)

    return ready_main_tasks_data

def process_parent_tasks_with_resolved_children(jira_client, issues, config, server_url):
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
                    subtask = jira_client.issue(subtask_ref.key, fields="status")
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

def process_simple_list(jira_client, issues, config, server_url):
    """Generic processor for filters that just need a list of keys/summaries/status."""
    results_data = []
    for issue in issues:
        basic_data = _extract_basic_issue_data(issue, server_url)
        basic_data['reason'] = basic_data['status']
        results_data.append(basic_data)
    return results_data

# --- JQL Builder ---
def build_jql(filter_id, user_params):
    """
    Constructs the JQL query based on filter definition and user parameters.
    Handles 'Any Assignee' option.
    Returns:
        tuple: (jql_string, effective_params)
    """
    from .config import FILTERS

    if filter_id not in FILTERS:
            raise ValueError(f"Filter ID '{filter_id}' not found in configuration.  ")

    filter_config = FILTERS[filter_id]
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

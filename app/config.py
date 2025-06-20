USER_SELECT_GROUP = "PMO" 

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
        "result_title": "My Tasks with All Sub-tasks Resolved",
        "order_by": "ORDER BY updated DESC"
    }
}
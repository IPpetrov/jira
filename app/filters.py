from .config import FILTERS
from .utils import sanitize_jql_list
from .jira_service import (
    _extract_basic_issue_data,
    process_ready_tasks,
    process_parent_tasks_with_resolved_children
)

class BaseFilter:
    """A base class for all Jira filters."""

    def __init__(self, filter_id, user_params, jira_client):
        if filter_id not in FILTERS:
            raise ValueError(f"Filter ID '{filter_id}' not found.")
            
        self.filter_id = filter_id
        self.config = FILTERS[filter_id]
        self.user_params = user_params
        self.jira_client = jira_client
        self.server_url = self.jira_client._options['server']
        self.effective_params = self._get_effective_params()
        self.jql = self._build_jql()

    def _get_effective_params(self):
        defaults = self.config.get('defaults', {})
        params = defaults.copy()
        params.update(self.user_params)
        for key in ['projects', 'exclude_types', 'include_types']:
            if key in params:
                value = params[key]
                if isinstance(value, str):
                    params[key] = [p.strip() for p in value.split(',') if p.strip()]
        return params

    def _build_jql(self):
        if 'assignee' not in self.effective_params or not self.effective_params['assignee']:
            self.effective_params['assignee'] = 'currentUser()'
        assignee_val = self.effective_params.get('assignee')
        if isinstance(assignee_val, str) and assignee_val not in ["currentUser()", "__any__"] and not assignee_val.startswith('"'):
             self.effective_params['assignee'] = f'"{assignee_val}"'
        jql_parts = []
        base_template = self.config.get('base_jql_template', '')
        if base_template:
            jql_parts.append(f"({base_template.format(**self.effective_params)})")
        if assignee_val and assignee_val != '__any__':
            jql_parts.append(f"assignee = {self.effective_params['assignee']}")
        if self.effective_params.get('projects'):
            jql_parts.append(f"project in ({sanitize_jql_list(self.effective_params['projects'])})")
        jql_string = " AND ".join(filter(None, jql_parts))
        order_by = self.config.get('order_by', 'ORDER BY updated DESC')
        if jql_string and order_by:
            jql_string += f" {order_by}"
        return jql_string
        
    def execute_search(self):
        if not self.jql: return []
        fields = self.config.get('fields', 'key,summary,status')
        expand = self.config.get('expand', 'fields.status.statusCategory')
        return self.jira_client.search_issues(self.jql, maxResults=50, fields=fields, expand=expand)

    def process_results(self, issues):
        results = []
        for issue in issues:
            data = _extract_basic_issue_data(issue, self.server_url)
            data['reason'] = data['status']
            results.append(data)
        return results

class ReadyTasksFilter(BaseFilter):
    def process_results(self, issues):
        return process_ready_tasks(self.jira_client, issues, self.effective_params, self.server_url)

class ParentsWithResolvedChildrenFilter(BaseFilter):
    def process_results(self, issues):
        return process_parent_tasks_with_resolved_children(self.jira_client, issues, self.effective_params, self.server_url)

FILTER_CLASS_MAP = {
    "ready_tasks": ReadyTasksFilter,
    "parents_resolved_children": ParentsWithResolvedChildrenFilter,
}

def get_filter_by_id(filter_id, user_params, jira_client):
    """Factory to get an instance of the correct filter class."""
    FilterClass = FILTER_CLASS_MAP.get(filter_id, BaseFilter)
    return FilterClass(filter_id, user_params, jira_client)
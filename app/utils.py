def sanitize_jql_list(input_list):
    """Takes a list of strings and formats them for a JQL 'in' clause."""
    if not input_list:
        return ""
    sanitized_items = [f'"{str(item).strip()}"' for item in input_list if str(item).strip()]
    if not sanitized_items:
        return ""
    return ", ".join(sanitized_items)
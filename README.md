# 🧩 Jira Task Filter (Flask App)

A simple internal Flask app that helps with advanced Jira filtering — specifically cases where Jira's JQL falls short.

### 🗂 What It Does

- Lets you filter and view Jira tickets that meet more complex conditions
- Example use case: find all parent tickets where **all linked subtasks are resolved**, so the parent can be closed
- Intended to make life easier when working with large Jira boards or BAU queues

---

### 🔧 Tech Stack

- **Python** + **Flask** for the web app
- **Jira REST API** for pulling ticket data
- **Docker** for easy packaging
- **Google Cloud Run** for serverless deployment

---

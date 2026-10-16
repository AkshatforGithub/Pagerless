import tools
for label, out in [
    ("containers", tools.container_status()),
    ("web detail", tools.container_status("web")),
    ("web logs", tools.get_logs("web", 10)),
    ("disk", tools.disk_usage("web")),
    ("metrics", tools.query_metrics()),
    ("probes", tools.probe_endpoints()),
    ("indexes", tools.run_diagnostic("pg_indexes")),
    ("env", tools.run_diagnostic("web_env")),
]:
    print(f"\n=== {label}\n{out}")

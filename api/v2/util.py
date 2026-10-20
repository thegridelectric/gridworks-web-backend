from datetime import datetime


def datetime_to_sema(dt: datetime):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

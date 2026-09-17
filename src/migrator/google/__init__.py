"""Thin per-API Google wrappers composed by connectors/google.py."""

# Retries handed to every googleapiclient ``execute()``: the library backs off
# exponentially on 5xx / 429 and transient socket errors. This is the only
# retry layer on the Google side (there is no GraphClient equivalent), so keep
# it on every call.
NUM_RETRIES = 3

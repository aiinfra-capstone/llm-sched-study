"""Log join: a pure function of (manifest, three log files) -> one Parquet (F-19).

No network, no engine, runnable on a laptop. This is what lets Person B hand over
a directory of *simulator* logs and have them processed with zero changes.
"""

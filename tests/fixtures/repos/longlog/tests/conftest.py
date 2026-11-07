"""Test setup for longlog.

After every run the suite prints a trace of 5,000 lines, the way a noisy CI log does. The
trace is there to test how an agent copes with output that is far bigger than its context window.
"""

NOISE_LINES = 5_000


def pytest_terminal_summary(terminalreporter) -> None:
    for n in range(1, NOISE_LINES + 1):
        terminalreporter.write_line(f"trace {n:04d}: audit event recorded, checksum ok")

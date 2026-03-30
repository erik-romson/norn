from __future__ import annotations

from norn.contrib.extractors.stacktrace import extract_stacktraces
from norn.contrib.extractors.class_names import extract_class_names


def test_extract_java_stacktrace():
    text = """Something happened.
java.lang.NullPointerException: Cannot invoke method
\tat com.acme.auth.TokenValidator.validate(TokenValidator.java:42)
\tat com.acme.auth.AuthService.login(AuthService.java:101)
\tat com.acme.web.LoginController.handleLogin(LoginController.java:55)
Some more text."""
    traces = extract_stacktraces(text)
    assert len(traces) >= 1
    assert "TokenValidator" in traces[0]


def test_extract_python_stacktrace():
    text = """Error occurred:
Traceback (most recent call last):
  File "app.py", line 10, in main
    process()
  File "processor.py", line 42, in process
    raise ValueError("bad input")
ValueError: bad input
End of log."""
    traces = extract_stacktraces(text)
    assert len(traces) >= 1
    assert "ValueError" in traces[0]


def test_extract_no_stacktrace():
    text = "Just a normal log message with no errors."
    traces = extract_stacktraces(text)
    assert traces == []


def test_extract_class_names_from_java():
    traces = [
        "at com.acme.auth.TokenValidator.validate(TokenValidator.java:42)\n"
        "at com.acme.auth.AuthService.login(AuthService.java:101)\n"
        "at java.lang.Thread.run(Thread.java:829)"
    ]
    classes = extract_class_names(traces)
    assert "com.acme.auth.TokenValidator" in classes
    assert "com.acme.auth.AuthService" in classes
    # Framework classes filtered out
    assert not any(c.startswith("java.") for c in classes)


def test_extract_class_names_empty():
    assert extract_class_names([]) == []

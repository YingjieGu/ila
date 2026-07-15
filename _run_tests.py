"""Direct test runner - no subprocess needed."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# Import and run pytest directly
import pytest
sys.exit(pytest.main([
    os.path.join(os.path.dirname(__file__), "tests", "core", "test_sandbox.py"),
    "-v", "--tb=short"
]))

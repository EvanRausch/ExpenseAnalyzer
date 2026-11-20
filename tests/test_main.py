"""
Tests for the main module
"""

from src.main import main


def test_main_runs():
    """Test that main function runs without errors."""
    # This is a basic test to get you started
    try:
        main()
        assert True
    except Exception as e:
        assert False, f"main() raised an exception: {e}"

"""Start AI Tutor.  python run.py            (background tutor)
                   python run.py --setup    (detect GPU/RAM, download the right model)
                   python run.py --hw       (show which model/device would be used)
                   python run.py --solve f  (solve a file without an editor, for testing)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ai_tutor.tutor import main  # noqa: E402

if __name__ == "__main__":
    main()

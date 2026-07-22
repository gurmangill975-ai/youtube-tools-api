import sys
import os

# Add root folder to sys.path so app and downloader modules are found reliably on Vercel
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from app import app

# Vercel entry point
app = app

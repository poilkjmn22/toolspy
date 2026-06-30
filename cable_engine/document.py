# cable_engine/document.py
from enum import Enum

class DocumentType(str, Enum):
    PDF = "pdf"
    DWG = "dwg"

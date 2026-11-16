"""Web novel import (Syosetu, Kakuyomu) normalized into the EPUB input path.

Downloaded episodes are cached with content hashes (checkpoint + change
detection), then synthesized into a Japanese EPUB3 so the existing Librarian
extracts them exactly like a commercial volume.
"""

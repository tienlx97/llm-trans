"""
NCX Generator - Creates EPUB2-compatible toc.ncx navigation.

Generates NCX (Navigation Control file for XML) for backward
compatibility with older e-readers that don't support EPUB3 navigation.
"""

from pathlib import Path
from dataclasses import dataclass, field
from typing import List
from html import escape


@dataclass
class NavPoint:
    """Navigation point for NCX navMap."""
    id: str
    label: str
    src: str           # Relative path from NCX location
    play_order: int
    children: List['NavPoint'] = field(default_factory=list)

    def to_xml(self, indent: int = 2) -> str:
        """Generate XML element for this nav point."""
        spaces = "  " * indent
        lines = [f'{spaces}<navPoint id="{self.id}" playOrder="{self.play_order}">']
        lines.append(f'{spaces}  <navLabel>')
        lines.append(f'{spaces}    <text>{escape(self.label)}</text>')
        lines.append(f'{spaces}  </navLabel>')
        lines.append(f'{spaces}  <content src="{escape(self.src)}"/>')

        # Nested nav points
        for child in self.children:
            lines.append(child.to_xml(indent + 1))

        lines.append(f'{spaces}</navPoint>')

        return "\n".join(lines)


class NCXGenerator:
    """Generates EPUB2-compatible toc.ncx files."""

    def __init__(self):
        """Initialize NCX generator."""
        pass

    def generate(
        self,
        output_path: Path,
        book_title: str,
        identifier: str,
        nav_points: List[NavPoint],
        depth: int = 1
    ) -> None:
        """
        Generate complete toc.ncx file.

        Args:
            output_path: Path to write toc.ncx
            book_title: Book title for docTitle
            identifier: Unique identifier (same as OPF)
            nav_points: List of navigation points
            depth: Maximum nesting depth
        """
        ncx_content = self._build_ncx(book_title, identifier, nav_points, depth)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(ncx_content, encoding="utf-8")

    def _build_ncx(
        self,
        book_title: str,
        identifier: str,
        nav_points: List[NavPoint],
        depth: int
    ) -> str:
        """Build complete NCX document."""
        nav_map = self._build_nav_map(nav_points)

        return f'''<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{escape(identifier)}"/>
    <meta name="dtb:depth" content="{depth}"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle>
    <text>{escape(book_title)}</text>
  </docTitle>
  <navMap>
{nav_map}
  </navMap>
</ncx>
'''

    def _build_nav_map(self, nav_points: List[NavPoint]) -> str:
        """Build navMap content from navigation points."""
        return "\n".join(np.to_xml(indent=2) for np in nav_points)

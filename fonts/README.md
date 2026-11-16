# Fonts embedded in Vietnamese EPUBs

| Role | Family | Version | Source | License |
|---|---|---|---|---|
| Body text | Literata (Regular, Bold, Italic, Bold Italic) | 3.103 | https://github.com/googlefonts/literata | SIL OFL 1.1 (`literata/OFL.txt`) |
| Headings | Inter (Regular, Bold) | 4.1 | https://github.com/rsms/inter | SIL OFL 1.1 (`inter/LICENSE.txt`) |

Both cover the full Vietnamese alphabet (all 134 tone/vowel letters, Đ/đ,
Ơ/ơ, Ư/ư). Neither license declares a Reserved Font Name.

The static TTFs were subset with fontTools to keep each EPUB small (about
1 MB of fonts, about 0.5 MB compressed, instead of 2.5 MB), keeping every
OpenType layout feature:

```
U+0020-007E, U+00A0-00FF, U+0100-024F, U+0300-0323, U+02BB-02BC, U+02C6,
U+02DA, U+02DC, U+1E00-1EFF, U+2000-206F, U+20AB, U+20AC, U+2122,
U+2190-2193, U+2212, U+2215, U+25A0-25FF, U+2600-26FF, U+FEFF, U+FFFD
```

Characters outside that set (Japanese in a title, rare symbols) are drawn by
the fallback fonts listed in `config.yaml` (`project.languages.vi.epub_fonts`).
To use other fonts, put the TTF files here and change that config block.

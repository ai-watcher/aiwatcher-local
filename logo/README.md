# Logo

`aiwatcher-mark.svg` is the source of truth for the mark.

The supplied artwork was three PNGs wrapped in an SVG shell — a single
`<image>` element carrying a base64 raster, plus clip rectangles. There was no
vector geometry in them to lift out, so the mark was rebuilt as real vector by
fitting rounded-rect rings to the 2000px raster. The fit agrees with the
original on 99.1% of pixels; the rest is anti-aliasing at the stroke edges.

## Geometry

Both rings are 300 wide, with a 40 stroke and an 85 outer corner radius. The
blue ring is 260 tall and the ink ring 232, offset by (129, 117). The height
difference is deliberate: equalising the two drops the pixel match against the
original from 99.1% to 89.5%.

The ink ring is drawn in front and is opaque — it occludes the blue ring rather
than notching it.

## Where it is used

| Surface | Form | Kept in step by |
|---|---|---|
| Dashboard header | inline `<svg>` in `aiwatcher_cli/web/index.html` | `test_the_brand_mark_is_the_logo` |
| Dashboard favicon | data-URI SVG in `index.html` and `faviconFor()` in `index.js` | `test_the_favicon_is_the_mark_carrying_the_state` |
| Browser extension | `browser-extension/icons/icon{16,48,128}.png` | `render_icons.py` |

The dashboard cannot reference a file here. `ui.py` splices the HTML, CSS and JS
into one self-contained page and the wheel ships only `web/*.{html,css,js}`, so
the mark has to be inline markup on those surfaces.

## Regenerating the extension icons

```bash
python3 logo/render_icons.py
```

Draws from the same geometry as the SVG rather than downsampling the original
artwork, which would bake in its white background. Output is transparent RGBA.

## Colours

| Token | Light | Dark |
|---|---|---|
| `--brand-blue` | `#0052F5` | `#0052F5` |
| `--brand-ink` | `#141314` | `#DCE6F6` |

The blue is fixed in both themes — it is the brand, and at 3.15:1 against the
dark ground it clears the 3:1 contrast floor for a graphic. The ink ring has to
invert or it disappears against a dark page, so in the dashboard it is drawn in
`currentColor`.

The favicon is the exception: it sits on the browser's tab strip rather than on
the page, so it cannot follow the page's theme. It asks the browser instead, via
a `prefers-color-scheme` rule inside the SVG, and falls back to the light ink on
browsers that do not resolve it.

## Known gap: the extension icon on a dark toolbar

`browser-extension/icons/*.png` are the light-ground mark. On a dark Chrome
toolbar the ink ring disappears and only the blue ring reads. A PNG cannot carry
a media query, so fixing it properly means a second set of files and a
`chrome.action.setIcon` swap driven by `matchMedia`. That is extension work
rather than logo work, and has not been done.

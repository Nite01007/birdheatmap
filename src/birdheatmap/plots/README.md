# Plot types

Each plot is a self-contained Python module in this directory.  The registry
in `__init__.py` discovers them automatically at startup — drop a new `.py`
file here, expose the required interface, and it appears in the UI immediately.

## Required interface

```python
NAME: str            # slug used in URLs, e.g. "annual_heatmap"
DISPLAY_NAME: str    # shown in the UI dropdown
DESCRIPTION: str     # tooltip / help text
PARAMS: list[dict]   # parameter specs (see below)

def render(db: sqlite3.Connection, species_id: int, **params) -> bytes:
    ...  # return PNG bytes
```

### Parameter spec dict keys

| Key       | Type   | Required | Notes                                            |
|-----------|--------|----------|--------------------------------------------------|
| `name`    | str    | yes      | query-string key                                 |
| `type`    | str    | yes      | controls the HTML input type (see table below)   |
| `label`   | str    | yes      | shown next to the input                          |
| `default` | any    | yes      | value used when the user omits the param         |
| `choices` | list\|None | no   | for `type="select"` — fixed option list          |

### Supported `type` values

| `type`          | HTML control                        | Notes |
|-----------------|-------------------------------------|-------|
| `"int"`         | `<input type="number">`             | Coerced to `int` before `render()` |
| `"float"`       | `<input type="number" step="0.5">`  | Coerced to `float` |
| `"select"`      | `<select>`                          | `choices` list required |
| `"year_or_all"` | `<select>` with years + All Years   | Value passed to `render()` is an `int` or the string `"all"` |

The `"year"` param name is special: the web layer populates its choices
dynamically from the years with data for the selected species.

## Implemented plots

- **annual_heatmap** — Year-long activity grid, one dot per 5-minute detection
  bucket, with sunrise/sunset curves overlaid.
- **all_years** — Same axes, every available year overlaid in distinct colours.
- **species_arrival_departure** — Horizontal bar chart of seasonal presence;
  bars span first–last detection date, IQR overlay shows middle 50%. Station-wide.
- **species_ridge** — Ridge/joy plot of detection day-of-year KDEs, sorted by
  peak date so the cascade reads Jan→Dec. Station-wide.
- **dawn_chorus** — Small-multiple histograms of detection time relative to local
  sunrise (−60 to +180 min), sorted by median singing time. Station-wide.

## Planned plots (not yet implemented)

- **hourly_density** — 24-hour polar histogram ("diurnal-pattern rose plot")
  showing detection counts by hour of day.
- **weather_overlay** — Like annual_heatmap but with temperature or precipitation
  data from a weather API blended into the background color.
- **multi_species** — Two or more species on the same annual grid, each in a
  distinct color, to compare activity windows.
- **year_over_year** — Stack multiple years of the same species vertically for
  direct comparison.
- **diurnal_rose** — Circular clock-face showing peak activity hours.

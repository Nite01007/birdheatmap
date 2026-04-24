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

| Key       | Type              | Required | Notes                                   |
|-----------|-------------------|----------|-----------------------------------------|
| `name`    | str               | yes      | query-string key                        |
| `type`    | "int"\|"str"\|"select" | yes | controls the HTML input type            |
| `label`   | str               | yes      | shown next to the input                 |
| `default` | any               | yes      | value used when the user omits the param|
| `choices` | list or None      | no       | for `type="select"` fixed option lists  |

## Implemented plots

- **annual_heatmap** — Year-long activity grid, one dot per 5-minute detection
  bucket, with sunrise/sunset curves overlaid.

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

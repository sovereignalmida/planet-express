"""Per-application widgets: one file each, declaring what to fetch and how to summarise it.

The contract is small on purpose. A widget file declares WIDGET -- name, the image repos it
matches, the env var holding its key, its port, and the GET paths it needs -- and a
summarise() that turns those responses into {stats, rows, rows_label, line}. The front end
renders every widget from that one shape, so adding a widget is adding a file.

Two rules hold for all of them:

  * READ-ONLY. A widget declares GET paths and the fetcher refuses anything else. This is what
    keeps faith with "nothing runs against a host without approval" -- a widget is a readout,
    not an action.
  * KEYS STAY ON THE HOST. `key` names an environment variable read from the host secrets
    file. The value never reaches the browser, is not editable from the dashboard, and is not
    in config.yaml. Same stance as sensitive config.
"""

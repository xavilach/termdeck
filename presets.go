package main

// Presets are partial layers for a device: the few settings that matter for a common kind of device. The
// form applies one to the device being edited, and shows what changed.

type preset struct {
	Name     string         `json:"name"`
	What     string         `json:"what"`
	Settings map[string]any `json:"settings"`
}

var presets = []preset{
	{"DOS text-mode (CP437)", "menus and frames drawn with the IBM PC character set", map[string]any{"charset": "cp437", "autoWrap": false}},
	{"Hex dump", "a binary protocol, byte by byte", map[string]any{"view": "hex", "localEcho": true, "txMarker": "TX "}},
	{"Shell (Zephyr, Linux, RTOS)", "a prompt with coloured log levels", map[string]any{"enter": "cr",
		"highlights": "/<err>|\\bERROR\\b/ = red+bold\n/<wrn>|\\bWARN(ING)?\\b/ = orange\n/<inf>/ = green"}},
	{"Log capture", "leave a device running and read later what it printed", map[string]any{"timestamps": true, "logTimestamps": true,
		"scrollback": 100000, "logPrintable": true}},
	{"Line instrument (SCPI)", "one command per line, answers on their own lines", map[string]any{"enter": "lf", "newLine": "lf", "localEcho": true}},
}

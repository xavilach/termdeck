package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"sync"
)

// Profile is a saved serial connection.
type Profile struct {
	Name      string `json:"name"`
	Device    string `json:"device"`
	Baud      int    `json:"baud"`
	DataBits  int    `json:"dataBits"`
	Parity    string `json:"parity"`   // none, even, odd, mark, space
	StopBits  string `json:"stopBits"` // 1, 1.5, 2
	Flow      string `json:"flow"`     // none, rtscts, xonxoff
	Enter     string `json:"enter"`    // what the Enter key sends: cr, lf, crlf
	Group     string `json:"group"`    // groups the profile in the list and picks its scripts
	LocalEcho bool   `json:"localEcho"`

	// Control lines at open: nil means asserted. Some boards reset when DTR changes.
	DTR *bool `json:"dtr"`
	RTS *bool `json:"rts"`

	// What the page does with the text (rarer options, behind "Advanced").
	ImplicitCR bool   `json:"implicitCR"` // a LF received also returns the cursor to the start of the line
	ImplicitLF bool   `json:"implicitLF"` // a CR received also moves down a line
	Answerback string `json:"answerback"` // sent when the device sends ENQ (Ctrl+E); empty sends nothing
	Colours    string `json:"colours"`    // NAME=#rrggbb per line, over the theme
	Bold       string `json:"bold"`       // how bold text shows: colour, font or both

	CopyOnSelect  bool   `json:"copyOnSelect"`
	RightClick    string `json:"rightClick"` // copypaste, paste or none
	NoMouseReport bool   `json:"noMouseReport"`
	AutoWrap      *bool  `json:"autoWrap"` // nil means on
	OriginMode    bool   `json:"originMode"`
	IgnoreTitle   bool   `json:"ignoreTitle"` // do not show the title the device sets
	BellTab       bool   `json:"bellTab"`     // flash the tab when the bell rings out of sight
	BellSound     string `json:"bellSound"`   // a sound as a data: URL, instead of the beep

	Charset     string `json:"charset"`   // what the device sends: utf-8 or a single-byte set
	WarnClose   *bool  `json:"warnClose"` // ask before closing the tab while connected; nil means yes
	Bell        string `json:"bell"`      // none, visual, sound, both
	Theme       string `json:"theme"`     // auto, dark, light
	FontSize    int    `json:"fontSize"`
	FontFamily  string `json:"fontFamily"`
	Cursor      string `json:"cursor"` // block, bar, underline
	CursorBlink bool   `json:"cursorBlink"`
	Scrollback  int    `json:"scrollback"` // lines kept

	// Markers put at the start of each received (Rx) or sent (Tx) line, on screen and in the log.
	// Empty means no marker.
	RxMarker     string `json:"rxMarker"`
	TxMarker     string `json:"txMarker"`
	LogPath      string `json:"logPath"` // empty means no log; {name}, {date} and {time} are replaced
	LogRxMarker  string `json:"logRxMarker"`
	LogExisting  string `json:"logExisting"`  // when the file exists: append, overwrite, ask
	LogPrintable bool   `json:"logPrintable"` // leave out escape sequences and control characters
	LogHeader    bool   `json:"logHeader"`    // start the log with a line saying when and what
	LogTxMarker  string `json:"logTxMarker"`

	// How received data is shown at first (the toolbar changes it for an open tab).
	View          string `json:"view"`          // text, hex, dec or bin
	Timestamps    bool   `json:"timestamps"`    // start each line with the time
	NewLine       string `json:"newLine"`       // what ends a received line: terminal, cr, lf, crlf, custom or none
	NewLineCustom string `json:"newLineCustom"` // the sequence for custom, with \r \n \t \xNN written out
	ShowControl   bool   `json:"showControl"`   // show CR, LF and other control characters as <CR>, <LF>, <ESC>...
	LinePauseMs   int    `json:"linePauseMs"`   // start a new line after this pause in the data; 0 means never
	Triggers      string `json:"triggers"`      // lines "text => reply": send the reply when the text is received
}

var (
	parities    = []string{"none", "even", "odd", "mark", "space"}
	stopBits    = []string{"1", "1.5", "2"}
	flows       = []string{"none", "rtscts", "xonxoff", "dsrdtr"}
	existings   = []string{"append", "overwrite", "ask"}
	bolds       = []string{"colour", "font", "both"}
	rightClicks = []string{"copypaste", "paste", "none"}
	enters      = []string{"cr", "lf", "crlf"}
	bells       = []string{"none", "visual", "sound", "both"}
	themes      = []string{"auto", "dark", "light"}
	cursors     = []string{"block", "bar", "underline"}
	views       = []string{"text", "hex", "dec", "bin"}
	newLines    = []string{"terminal", "cr", "lf", "crlf", "custom", "none"}
	dataBitsS   = []int{5, 6, 7, 8}
)

func oneOf(value string, choices []string) bool {
	for _, c := range choices {
		if c == value {
			return true
		}
	}
	return false
}

// Normalize fills defaults and rejects values the port cannot use.
func (p *Profile) Normalize() error {
	p.Name = strings.TrimSpace(p.Name)
	p.Device = strings.TrimSpace(p.Device)
	p.Group = cleanGroup(p.Group)
	p.LogPath = strings.TrimSpace(p.LogPath)
	if p.Device == "" {
		return errors.New("device is required")
	}
	if p.Baud == 0 {
		p.Baud = 115200
	}
	if p.Baud < 50 || p.Baud > 10_000_000 {
		return fmt.Errorf("baud rate %d is out of range", p.Baud)
	}
	if p.DataBits == 0 {
		p.DataBits = 8
	}
	ok := false
	for _, d := range dataBitsS {
		ok = ok || d == p.DataBits
	}
	if !ok {
		return fmt.Errorf("data bits must be 5, 6, 7 or 8, not %d", p.DataBits)
	}
	for _, f := range []struct {
		field   *string
		def     string
		choices []string
		what    string
	}{
		{&p.Parity, "none", parities, "parity"},
		{&p.StopBits, "1", stopBits, "stop bits"},
		{&p.Flow, "none", flows, "flow control"},
		{&p.Enter, "cr", enters, "enter key"},
		{&p.Charset, "utf-8", charsetNames(), "character set"},
		{&p.Bold, "colour", bolds, "bold text"},
		{&p.RightClick, "copypaste", rightClicks, "right click"},
		{&p.LogExisting, "append", existings, "log file handling"},
		{&p.Bell, "visual", bells, "bell"},
		{&p.Theme, "auto", themes, "theme"},
		{&p.Cursor, "block", cursors, "cursor"},
		{&p.View, "text", views, "view"},
		{&p.NewLine, "terminal", newLines, "new line"},
	} {
		if *f.field == "" {
			*f.field = f.def
		}
		if !oneOf(*f.field, f.choices) {
			return fmt.Errorf("%s must be one of %s", f.what, strings.Join(f.choices, ", "))
		}
	}
	if p.FontSize == 0 {
		p.FontSize = 14
	}
	if p.FontSize < 6 || p.FontSize > 72 {
		return fmt.Errorf("font size %d is out of range (6 to 72)", p.FontSize)
	}
	if p.Scrollback == 0 {
		p.Scrollback = 10000
	}
	if p.Scrollback < 100 || p.Scrollback > 1_000_000 {
		return fmt.Errorf("scrollback %d is out of range (100 to 1000000)", p.Scrollback)
	}
	p.FontFamily = strings.TrimSpace(p.FontFamily)
	if len(p.Answerback) > 255 {
		return errors.New("the answerback is limited to 255 characters")
	}
	if err := checkColours(p.Colours); err != nil {
		return err
	}
	if len(p.NewLineCustom) > 64 {
		return errors.New("the custom new line is limited to 64 characters")
	}
	if p.LinePauseMs < 0 || p.LinePauseMs > 60_000 {
		return fmt.Errorf("new line pause %d ms is out of range (0 to 60000)", p.LinePauseMs)
	}
	if _, err := parseTriggers(p.Triggers); err != nil {
		return err
	}
	if p.BellSound != "" && (len(p.BellSound) > 400_000 || !strings.HasPrefix(p.BellSound, "data:audio/")) {
		return errors.New("the bell sound must be an audio file of at most 300 KB")
	}
	return nil
}

var colourNames = []string{"foreground", "background", "cursor", "selection", "black", "red", "green", "yellow", "blue",
	"magenta", "cyan", "white", "brightBlack", "brightRed", "brightGreen", "brightYellow", "brightBlue",
	"brightMagenta", "brightCyan", "brightWhite"}

var hexColour = regexp.MustCompile(`^#[0-9a-fA-F]{6}$`)

// checkColours accepts lines such as "background=#101820"; empty lines are fine.
func checkColours(text string) error {
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		name, value, ok := strings.Cut(line, "=")
		name, value = strings.TrimSpace(name), strings.TrimSpace(value)
		if !ok || !oneOf(name, colourNames) || !hexColour.MatchString(value) {
			return fmt.Errorf("colour line %q: use NAME=#rrggbb with a name from %s", line, strings.Join(colourNames, ", "))
		}
	}
	return nil
}

// asserted reports whether a control line option is on; unset means on.
func asserted(b *bool) bool { return b == nil || *b }

// generalGroup is how the page shows the empty group, the one for items without a group.
const generalGroup = "General"

// cleanGroup trims a group name; "General" is the empty group, so it is stored as "".
func cleanGroup(name string) string {
	name = strings.TrimSpace(name)
	if strings.EqualFold(name, generalGroup) {
		return ""
	}
	return name
}

// Script is a list of commands sent one after the other, with a pause between them.
type Script struct {
	Name     string   `json:"name"`
	Group    string   `json:"group"` // shown for profiles of this group; empty means every group
	Commands []string `json:"commands"`
	DelayMs  int      `json:"delayMs"` // pause after each command
	Loop     bool     `json:"loop"`
	Count    int      `json:"count"` // how many rounds when looping; 0 means until stopped
}

func (s *Script) Normalize() error {
	s.Name = strings.TrimSpace(s.Name)
	s.Group = cleanGroup(s.Group)
	for len(s.Commands) > 0 && strings.TrimSpace(s.Commands[len(s.Commands)-1]) == "" {
		s.Commands = s.Commands[:len(s.Commands)-1]
	}
	if len(s.Commands) == 0 {
		return errors.New("a script needs at least one command")
	}
	for _, command := range s.Commands {
		if _, _, err := scriptSteps(command); err != nil {
			return fmt.Errorf("script line %q: %w", command, err)
		}
	}
	if s.DelayMs < 0 || s.DelayMs > 3_600_000 {
		return fmt.Errorf("delay %d ms is out of range (0 to 3600000)", s.DelayMs)
	}
	if s.Count < 0 {
		return errors.New("count cannot be negative")
	}
	return nil
}

// Store keeps named items (profiles or scripts) in one JSON file, readable by the owner only.
type Store[T any] struct {
	mu        sync.Mutex
	path      string
	name      func(*T) string
	normalize func(*T) error
	what      string
}

func NewStore(path string) *Store[Profile] {
	return &Store[Profile]{path: path, what: "profile",
		name: func(p *Profile) string { return p.Name }, normalize: (*Profile).Normalize}
}

func NewScriptStore(path string) *Store[Script] {
	return &Store[Script]{path: path, what: "script",
		name: func(s *Script) string { return s.Name }, normalize: (*Script).Normalize}
}

// NewGroupStore keeps the names of groups, so a group can exist before it has sessions or scripts.
func NewGroupStore(path string) *Store[string] {
	return &Store[string]{path: path, what: "group",
		name: func(g *string) string { return *g },
		normalize: func(g *string) error {
			*g = strings.TrimSpace(*g)
			if strings.EqualFold(*g, generalGroup) {
				return errors.New(`"General" always exists`)
			}
			return nil
		}}
}

func configDir() (string, error) {
	if dir := os.Getenv("TERMDECK_HOME"); dir != "" {
		return dir, nil
	}
	base, err := os.UserConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(base, "termdeck"), nil
}

func (s *Store[T]) load() ([]T, error) {
	data, err := os.ReadFile(s.path)
	if errors.Is(err, os.ErrNotExist) {
		return []T{}, nil
	}
	if err != nil {
		return nil, err
	}
	var items []T
	if err := json.Unmarshal(data, &items); err != nil {
		return nil, fmt.Errorf("%s is not valid: %w", s.path, err)
	}
	return items, nil
}

func (s *Store[T]) save(items []T) error {
	sort.Slice(items, func(i, j int) bool {
		return strings.ToLower(s.name(&items[i])) < strings.ToLower(s.name(&items[j]))
	})
	data, err := json.MarshalIndent(items, "", "  ")
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0o700); err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, s.path)
}

func (s *Store[T]) List() ([]T, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.load()
}

// Get returns the item called name.
func (s *Store[T]) Get(name string) (T, error) {
	items, err := s.List()
	var zero T
	if err != nil {
		return zero, err
	}
	for i := range items {
		if s.name(&items[i]) == name {
			return items[i], nil
		}
	}
	return zero, fmt.Errorf("no %s named %q", s.what, name)
}

// Put saves item under its name; oldName (when not empty) is the item being edited or renamed.
func (s *Store[T]) Put(oldName string, item T) error {
	if err := s.normalize(&item); err != nil {
		return err
	}
	name := s.name(&item)
	if name == "" {
		return errors.New("name is required")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	items, err := s.load()
	if err != nil {
		return err
	}
	out := make([]T, 0, len(items)+1)
	found := oldName == ""
	for i := range items {
		other := s.name(&items[i])
		switch {
		case other == oldName && oldName != "":
			found = true
		case strings.EqualFold(other, name):
			return fmt.Errorf("a %s named %q already exists", s.what, other)
		default:
			out = append(out, items[i])
		}
	}
	if !found {
		return fmt.Errorf("no %s named %q", s.what, oldName)
	}
	return s.save(append(out, item))
}

func (s *Store[T]) Delete(name string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	items, err := s.load()
	if err != nil {
		return err
	}
	out := items[:0]
	for i := range items {
		if s.name(&items[i]) != name {
			out = append(out, items[i])
		}
	}
	if len(out) == len(items) {
		return fmt.Errorf("no %s named %q", s.what, name)
	}
	return s.save(out)
}

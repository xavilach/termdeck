package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"net/http"
	"slices"
	"strings"
)

// Devices and ports: the settings of a port come in layers. Defaults, then Generic (what the user changed for
// every port), then the device opened on the port (what that kind of device speaks), then the port's own
// changes. Each layer holds only the fields it sets, as JSON names of Profile; the session gets the merge.

// genericDevice is the device every port starts with; it always exists and holds the defaults the user changed.
const genericDevice = "Generic"

// Device is a kind of device: the settings every port opened with it starts from.
type Device struct {
	Name     string         `json:"name"`
	Settings map[string]any `json:"settings"`
}

// PortEntry is a port termdeck has seen: the device it was last opened with, and its own changes.
type PortEntry struct {
	Port     string         `json:"port"`     // COM3, /dev/ttyUSB0, fake:echo
	Serial   string         `json:"serial"`   // the serial number of its USB adapter, when it has one: the port is found by it
	Device   string         `json:"device"`   // the device it was last opened with; empty is Generic
	Settings map[string]any `json:"settings"` // what differs from the device
}

// identityKeys are the fields of a Profile that are not settings: they say which port, not how.
var identityKeys = []string{"name", "device", "group", "peer"}

// defaultSettings is every setting with its default value, as JSON.
func defaultSettings() map[string]any {
	p := Profile{Device: "x"}
	p.Normalize()
	m := toMap(p)
	for _, k := range identityKeys {
		delete(m, k)
	}
	return m
}

func toMap(v any) map[string]any {
	data, _ := json.Marshal(v)
	m := map[string]any{}
	json.Unmarshal(data, &m)
	return m
}

// checkLayer refuses names that are not settings, and values the settings refuse.
func checkLayer(layer map[string]any) error {
	defaults := defaultSettings()
	for key := range layer {
		if _, ok := defaults[key]; !ok {
			return fmt.Errorf("%q is not a setting", key)
		}
	}
	_, err := mergeProfile("x", defaults, layer)
	return err
}

// mergeProfile is the profile of port with the layers applied in order (the later ones win), checked.
func mergeProfile(port string, layers ...map[string]any) (Profile, error) {
	m := map[string]any{}
	for _, layer := range layers {
		maps.Copy(m, layer)
	}
	m["device"] = port
	data, _ := json.Marshal(m)
	var p Profile
	if err := json.Unmarshal(data, &p); err != nil {
		return Profile{}, err
	}
	return p, p.Normalize()
}

// settingsDiff keeps the settings of full that differ from base: what a layer must hold to give full over base.
func settingsDiff(full, base map[string]any) map[string]any {
	out := map[string]any{}
	for key, value := range full {
		if slices.Contains(identityKeys, key) {
			continue
		}
		if b, ok := base[key]; !ok || fmt.Sprint(b) != fmt.Sprint(value) {
			out[key] = value
		}
	}
	return out
}

func newDeviceStore(path string) *Store[Device] {
	return &Store[Device]{path: path, what: "device", name: func(d *Device) string { return d.Name },
		normalize: func(d *Device) error {
			d.Name = strings.TrimSpace(d.Name)
			if d.Settings == nil {
				d.Settings = map[string]any{}
			}
			if err := checkLayer(d.Settings); err != nil {
				return fmt.Errorf("device %s: %w", d.Name, err)
			}
			return nil
		}}
}

func newPortStore(path string) *Store[PortEntry] {
	return &Store[PortEntry]{path: path, what: "port", name: func(p *PortEntry) string { return portKey(p.Port) },
		normalize: func(p *PortEntry) error {
			p.Port = strings.TrimSpace(p.Port)
			if p.Settings == nil {
				p.Settings = map[string]any{}
			}
			if strings.EqualFold(p.Device, genericDevice) {
				p.Device = ""
			}
			if err := checkLayer(p.Settings); err != nil {
				return fmt.Errorf("port %s: %w", p.Port, err)
			}
			return nil
		}}
}

// device is the device of that name; Generic (or an empty name, or one that is gone) is the Generic device.
func (s *Server) device(name string) Device {
	if name != "" && !strings.EqualFold(name, genericDevice) {
		if d, err := s.devices.Get(name); err == nil {
			return d
		}
	}
	if d, err := s.devices.Get(genericDevice); err == nil {
		return d
	}
	return Device{Name: genericDevice, Settings: map[string]any{}}
}

// portEntry is what termdeck keeps about a port: found by the serial number of its adapter first, then by name.
func (s *Server) portEntry(port, serial string) PortEntry {
	entries, _ := s.portEntries.List()
	if serial != "" {
		for _, e := range entries {
			if e.Serial == serial {
				e.Port = port // the adapter is on another port now
				return e
			}
		}
	}
	for _, e := range entries {
		if portKey(e.Port) == portKey(port) {
			return e
		}
	}
	return PortEntry{Port: port, Serial: serial, Settings: map[string]any{}}
}

// layers is what the page needs to open a port or edit a layer: each layer, and the merge of all of them.
type layers struct {
	Defaults map[string]any `json:"defaults"`
	Generic  map[string]any `json:"generic"`
	Device   Device         `json:"device"`
	Port     *PortEntry     `json:"port,omitempty"`
	Profile  *Profile       `json:"profile,omitempty"` // the settings a session of the port gets
	Error    string         `json:"error,omitempty"`
}

// sessionName is how a tab with a port and its device is called.
func sessionName(port string, device Device) string {
	if device.Name == genericDevice {
		return port
	}
	return device.Name + " · " + port
}

// resolveLayers gives the layers of a port opened with a device (the port's last device when device is empty),
// or of a device alone when port is empty.
func (s *Server) resolveLayers(port, serial, device string) layers {
	out := layers{Defaults: defaultSettings(), Generic: s.device(genericDevice).Settings}
	if port == "" {
		out.Device = s.device(device)
		return out
	}
	entry := s.portEntry(port, serial)
	if device == "" {
		device = entry.Device
	}
	out.Device = s.device(device)
	out.Port = &entry
	p, err := mergeProfile(port, out.Defaults, out.Generic, out.Device.Settings, entry.Settings)
	if err != nil {
		out.Error = err.Error()
		return out
	}
	p.Name = sessionName(port, out.Device)
	out.Profile = &p
	return out
}

// ---------------------------------------------------------------- HTTP

func (s *Server) listDevices(w http.ResponseWriter, r *http.Request) {
	devices, err := s.devices.List()
	out := []Device{s.device(genericDevice)}
	for _, d := range devices {
		if d.Name != genericDevice {
			out = append(out, d)
		}
	}
	data := map[string]any{"devices": out}
	if err != nil {
		data["error"] = err.Error()
	}
	writeJSON(w, http.StatusOK, data)
}

// saveDevice stores a device (PUT /api/devices/{name} renames it, and its ports and scripts follow).
func (s *Server) saveDevice(w http.ResponseWriter, r *http.Request) {
	var d Device
	if !readJSON(w, r, &d) {
		return
	}
	old := r.PathValue("name")
	if d.Name == "" {
		writeError(w, http.StatusBadRequest, errors.New("a device needs a name"))
		return
	}
	generic := strings.EqualFold(old, genericDevice)
	if generic != strings.EqualFold(d.Name, genericDevice) {
		writeError(w, http.StatusBadRequest, errors.New("Generic keeps its name, and no other device can take it"))
		return
	}
	if generic {
		d.Name = genericDevice
		if _, err := s.devices.Upsert(d); err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
		writeJSON(w, http.StatusOK, d)
		return
	}
	if err := s.devices.Put(old, d); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if old != "" && old != d.Name {
		s.renameDevice(old, d.Name)
	}
	writeJSON(w, http.StatusOK, d)
}

// renameDevice makes the ports and scripts of a device follow its new name.
func (s *Server) renameDevice(old, name string) {
	entries, _ := s.portEntries.List()
	for _, e := range entries {
		if e.Device == old {
			e.Device = name
			s.portEntries.Upsert(e)
		}
	}
	scripts, _ := s.scripts.List()
	for _, sc := range scripts {
		if sc.Device == old {
			sc.Device = name
			s.scripts.Put(sc.Name, sc)
		}
	}
}

// deleteDevice forgets a device that has no scripts; its ports go back to Generic.
func (s *Server) deleteDevice(w http.ResponseWriter, r *http.Request) {
	name := r.PathValue("name")
	if strings.EqualFold(name, genericDevice) {
		writeError(w, http.StatusBadRequest, errors.New("Generic cannot be deleted"))
		return
	}
	scripts, _ := s.scripts.List()
	for _, sc := range scripts {
		if sc.Device == name {
			writeError(w, http.StatusConflict, fmt.Errorf("device %q still has scripts (%s first)", name, sc.Name))
			return
		}
	}
	if err := s.devices.Delete(name); err != nil {
		writeError(w, http.StatusNotFound, err)
		return
	}
	s.renameDevice(name, "")
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

func (s *Server) listPortEntries(w http.ResponseWriter, r *http.Request) {
	entries, err := s.portEntries.List()
	data := map[string]any{"ports": entries}
	if err != nil {
		data["error"] = err.Error()
	}
	writeJSON(w, http.StatusOK, data)
}

// savePortEntry stores what termdeck keeps about a port: its device and its own changes.
func (s *Server) savePortEntry(w http.ResponseWriter, r *http.Request) {
	var e PortEntry
	if !readJSON(w, r, &e) {
		return
	}
	if e.Port == "" {
		writeError(w, http.StatusBadRequest, errors.New("which port?"))
		return
	}
	if e.Device != "" {
		if _, err := s.devices.Get(e.Device); err != nil && !strings.EqualFold(e.Device, genericDevice) {
			writeError(w, http.StatusBadRequest, err)
			return
		}
	}
	if e.Serial != "" { // the adapter was known under another port name: that entry is this one now
		entries, _ := s.portEntries.List()
		for _, old := range entries {
			if old.Serial == e.Serial && portKey(old.Port) != portKey(e.Port) {
				s.portEntries.Delete(portKey(old.Port))
			}
		}
	}
	if _, err := s.portEntries.Upsert(e); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, e)
}

func (s *Server) deletePortEntry(w http.ResponseWriter, r *http.Request) {
	if err := s.portEntries.Delete(portKey(r.PathValue("port"))); err != nil {
		writeError(w, http.StatusNotFound, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

// getLayers answers GET /api/layers?port=COM3&serial=...&device=Board.
func (s *Server) getLayers(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	writeJSON(w, http.StatusOK, s.resolveLayers(q.Get("port"), q.Get("serial"), q.Get("device")))
}

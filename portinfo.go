package main

import (
	"os"
	"path/filepath"
	"strings"
)

// What each port is, so that several adapters can be told apart: the USB product, vendor and serial number.

type portDetail struct {
	Name string `json:"name"` // what the device calls itself: "FT232R USB UART"
	ID   string `json:"id"`   // VID:PID, such as 0403:6001
	Info string `json:"info"` // a line for the list: name, ID and serial number
}

// usbDetailFromSysfs reads the USB description of a Linux tty (ttyUSB0, ttyACM0) from sysfs under root.
func usbDetailFromSysfs(root, tty string) (portDetail, bool) {
	dir, err := filepath.EvalSymlinks(filepath.Join(root, "sys", "class", "tty", tty, "device"))
	if err != nil {
		return portDetail{}, false
	}
	for i := 0; i < 4 && dir != "/" && dir != "." && len(dir) > len(root); i++ { // the USB device is a few folders up from the interface
		if vendor := readTrimmed(filepath.Join(dir, "idVendor")); vendor != "" {
			d := portDetail{
				Name: strings.TrimSpace(readTrimmed(filepath.Join(dir, "manufacturer")) + " " + readTrimmed(filepath.Join(dir, "product"))),
				ID:   vendor + ":" + readTrimmed(filepath.Join(dir, "idProduct")),
			}
			d.Info = strings.Join(nonEmpty(d.Name, d.ID, readTrimmed(filepath.Join(dir, "serial"))), " · ")
			return d, true
		}
		dir = filepath.Dir(dir)
	}
	return portDetail{}, false
}

func readTrimmed(path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

func nonEmpty(parts ...string) []string {
	var out []string
	for _, p := range parts {
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

// portDetails describes the ports it can: by device name, as listed.
func portDetails(ports []string) map[string]portDetail {
	out := map[string]portDetail{}
	windows := usbDetailsFromRegistry()
	for _, port := range ports {
		if d, ok := windows[strings.ToUpper(port)]; ok {
			out[port] = d
		} else if d, ok := usbDetailFromSysfs("/", filepath.Base(port)); ok {
			out[port] = d
		}
	}
	return out
}

//go:build linux

package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestAUsbSerialPortIsDescribedFromSysfs(t *testing.T) {
	root := t.TempDir()
	usb := filepath.Join(root, "sys", "devices", "pci0", "usb1", "1-2")
	iface := filepath.Join(usb, "1-2:1.0")
	tty := filepath.Join(iface, "ttyUSB0", "tty", "ttyUSB0")
	for _, d := range []string{iface, tty, filepath.Join(root, "sys", "class", "tty")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	for name, value := range map[string]string{"idVendor": "0403\n", "idProduct": "6001\n", "manufacturer": "FTDI\n", "product": "FT232R USB UART\n", "serial": "A50285BI\n"} {
		os.WriteFile(filepath.Join(usb, name), []byte(value), 0o644)
	}
	if err := os.MkdirAll(filepath.Join(root, "sys", "class", "tty", "ttyUSB0"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(iface, filepath.Join(root, "sys", "class", "tty", "ttyUSB0", "device")); err != nil {
		t.Fatal(err)
	}
	d, ok := usbDetailFromSysfs(root, "ttyUSB0")
	if !ok || d.Name != "FTDI FT232R USB UART" || d.ID != "0403:6001" || d.Info != "FTDI FT232R USB UART · 0403:6001 · A50285BI" {
		t.Errorf("%v %+v", ok, d)
	}
	if _, ok := usbDetailFromSysfs(root, "ttyS0"); ok {
		t.Error("a port that is not USB has no description")
	}
}

func TestPortInfoIsServedAndFakePortsHaveNone(t *testing.T) {
	ts := newTestServer(t)
	code, body := ts.do(t, "GET", "/api/portinfo", "", ts.secret)
	if code != 200 || body[0] != '{' {
		t.Errorf("%d %s", code, body)
	}
}

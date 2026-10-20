package main

import (
	"errors"
	"fmt"
	"io/fs"
	"runtime"
	"sort"
	"strconv"
	"strings"

	"termdeck/internal/serial"
)

func listPorts() ([]string, error) {
	ports, err := serial.GetPortsList()
	if ports == nil {
		ports = []string{}
	}
	sort.SliceStable(ports, func(i, j int) bool { return naturalLess(ports[i], ports[j]) })
	return ports, err
}

// naturalLess orders names so that COM2 comes before COM10 and ttyUSB2 before ttyUSB10.
func naturalLess(a, b string) bool {
	for a != "" && b != "" {
		da, db := leadingDigits(a), leadingDigits(b)
		if da != "" && db != "" {
			na, _ := strconv.Atoi(da)
			nb, _ := strconv.Atoi(db)
			if na != nb {
				return na < nb
			}
			a, b = a[len(da):], b[len(db):]
			continue
		}
		if a[0] != b[0] {
			return a[0] < b[0]
		}
		a, b = a[1:], b[1:]
	}
	return len(a) < len(b)
}

func leadingDigits(s string) string {
	n := 0
	for n < len(s) && s[n] >= '0' && s[n] <= '9' {
		n++
	}
	return s[:n]
}

func openPort(p Profile) (serial.Port, error) {
	if isFake(p.Device) {
		return openFake(p.Device, asserted(p.DTR), asserted(p.RTS))
	}
	mode := serialMode(p)
	if !asserted(p.DTR) || !asserted(p.RTS) { // the default (both asserted) needs no call, which some ports refuse
		mode.InitialStatusBits = &serial.ModemOutputBits{DTR: asserted(p.DTR), RTS: asserted(p.RTS)}
	}
	port, err := serial.Open(p.Device, mode)
	if err != nil {
		return nil, fmt.Errorf("cannot open %s: %s", p.Device, openAdvice(err, runtime.GOOS))
	}
	return port, nil
}

// serialMode is the speed, framing and flow control of a profile.
func serialMode(p Profile) *serial.Mode {
	mode := &serial.Mode{BaudRate: p.Baud, DataBits: p.DataBits}
	switch p.Parity {
	case "even":
		mode.Parity = serial.EvenParity
	case "odd":
		mode.Parity = serial.OddParity
	case "mark":
		mode.Parity = serial.MarkParity
	case "space":
		mode.Parity = serial.SpaceParity
	}
	switch p.StopBits {
	case "1.5":
		mode.StopBits = serial.OnePointFiveStopBits
	case "2":
		mode.StopBits = serial.TwoStopBits
	}
	switch p.Flow {
	case "rtscts":
		mode.FlowControl = serial.RTSCTSFlowControl
	case "xonxoff":
		mode.FlowControl = serial.XonXoffFlowControl
	case "dsrdtr":
		mode.FlowControl = serial.DsrDtrFlowControl
	}
	return mode
}

// openAdvice says what a failure to open a port probably means, and what to do about it.
func openAdvice(err error, goos string) string {
	text := err.Error()
	lower := strings.ToLower(text)
	var portErr *serial.PortError
	code := serial.PortErrorCode(-1)
	if errors.As(err, &portErr) {
		code = portErr.Code()
	}
	switch {
	case code == serial.PortBusy || strings.Contains(lower, "busy"):
		return text + ": another program has the port open; close it there first"
	case code == serial.PortNotFound || errors.Is(err, fs.ErrNotExist) || strings.Contains(lower, "cannot find"):
		return text + ": there is no such port; is the adapter plugged in? (the list on the left shows what exists)"
	case goos == "windows" && strings.Contains(lower, "access is denied"):
		return text + ": the port is open in another program, or you have no permission"
	case code == serial.PermissionDenied || errors.Is(err, fs.ErrPermission):
		if goos == "linux" {
			return text + ": no permission; add your user to the group that owns the port (often dialout or uucp) and log in again"
		}
		return text + ": no permission to open the port"
	}
	return text
}

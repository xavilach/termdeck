package main

import (
	"fmt"
	"sort"
	"strconv"

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
	mode := &serial.Mode{BaudRate: p.Baud, DataBits: p.DataBits}
	if !asserted(p.DTR) || !asserted(p.RTS) { // the default (both asserted) needs no call, which some ports refuse
		mode.InitialStatusBits = &serial.ModemOutputBits{DTR: asserted(p.DTR), RTS: asserted(p.RTS)}
	}
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
	port, err := serial.Open(p.Device, mode)
	if err != nil {
		return nil, fmt.Errorf("cannot open %s: %w", p.Device, err)
	}
	return port, nil
}

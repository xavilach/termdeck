package main

import (
	"fmt"

	"termdeck/internal/serial"
)

func listPorts() ([]string, error) {
	ports, err := serial.GetPortsList()
	if ports == nil {
		ports = []string{}
	}
	return ports, err
}

func openPort(p Profile) (serial.Port, error) {
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

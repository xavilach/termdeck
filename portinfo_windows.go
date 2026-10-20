//go:build windows

package main

import (
	"strings"

	"golang.org/x/sys/windows/registry"
)

// usbDetailsFromRegistry reads the USB devices Windows knows (HKLM\SYSTEM\CurrentControlSet\Enum\USB) and
// gives their description by the COM port they got.
func usbDetailsFromRegistry() map[string]portDetail {
	out := map[string]portDetail{}
	root, err := registry.OpenKey(registry.LOCAL_MACHINE, `SYSTEM\CurrentControlSet\Enum\USB`, registry.ENUMERATE_SUB_KEYS)
	if err != nil {
		return out
	}
	defer root.Close()
	kinds, _ := root.ReadSubKeyNames(-1)
	for _, kind := range kinds { // VID_0403&PID_6001
		kindKey, err := registry.OpenKey(root, kind, registry.ENUMERATE_SUB_KEYS)
		if err != nil {
			continue
		}
		instances, _ := kindKey.ReadSubKeyNames(-1)
		for _, instance := range instances {
			key, err := registry.OpenKey(kindKey, instance, registry.QUERY_VALUE)
			if err != nil {
				continue
			}
			friendly, _, _ := key.GetStringValue("FriendlyName")
			if friendly == "" {
				friendly, _, _ = key.GetStringValue("DeviceDesc")
				if at := strings.LastIndex(friendly, ";"); at >= 0 {
					friendly = friendly[at+1:]
				}
			}
			key.Close()
			params, err := registry.OpenKey(kindKey, instance+`\Device Parameters`, registry.QUERY_VALUE)
			if err != nil {
				continue
			}
			portName, _, _ := params.GetStringValue("PortName")
			params.Close()
			if portName == "" {
				continue
			}
			id := strings.NewReplacer("VID_", "", "PID_", "", "&", ":").Replace(strings.SplitN(kind, "&MI", 2)[0])
			d := portDetail{Name: strings.TrimSpace(friendly), ID: id}
			if !strings.Contains(instance, "&") { // an instance without & is the serial number of the adapter
				d.Serial = instance
			}
			d.Info = strings.Join(nonEmpty(d.Name, d.ID, d.Serial), " · ")
			out[strings.ToUpper(portName)] = d
		}
		kindKey.Close()
	}
	return out
}

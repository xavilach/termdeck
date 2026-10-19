//go:build windows

package main

import (
	"strconv"

	"golang.org/x/sys/windows/registry"
)

// puttySessionsFromRegistry reads the sessions PuTTY saved under HKEY_CURRENT_USER\Software\SimonTatham\PuTTY\Sessions.
func puttySessionsFromRegistry() []Profile {
	root, err := registry.OpenKey(registry.CURRENT_USER, `Software\SimonTatham\PuTTY\Sessions`, registry.ENUMERATE_SUB_KEYS)
	if err != nil {
		return nil
	}
	defer root.Close()
	names, _ := root.ReadSubKeyNames(-1)
	var out []Profile
	for _, name := range names {
		key, err := registry.OpenKey(root, name, registry.QUERY_VALUE)
		if err != nil {
			continue
		}
		settings := map[string]string{}
		for _, field := range []string{"Protocol", "SerialLine"} {
			if v, _, err := key.GetStringValue(field); err == nil {
				settings[field] = v
			}
		}
		for _, field := range []string{"SerialSpeed", "SerialDataBits", "SerialStopHalfbits", "SerialParity", "SerialFlowControl"} {
			if v, _, err := key.GetIntegerValue(field); err == nil {
				settings[field] = strconv.FormatUint(v, 10)
			}
		}
		key.Close()
		if p, ok := puttyProfile(name, settings); ok {
			out = append(out, p)
		}
	}
	return out
}

//go:build windows && amd64

package supervisor

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"zvukbook/desktop/internal/winhost"
)

type desktopSettings struct {
	Schema string `json:"schema"`
	Device string `json:"device"`
}

func (s *Supervisor) loadSettings() error {
	path := filepath.Join(s.Data, "desktop-settings.json")
	info, err := os.Stat(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if info.Size() > 4096 {
		return fmt.Errorf("Повреждены настройки устройства: %s", path)
	}
	bytes, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var settings desktopSettings
	if json.Unmarshal(bytes, &settings) != nil || settings.Schema != "zvukbook-device-v1" ||
		(settings.Device != "cpu" && settings.Device != "cuda") {
		return fmt.Errorf("Неизвестные настройки устройства. Книги не изменены.")
	}
	if settings.Device == "cuda" && !s.cudaEnabled {
		// Do not silently run an old GPU selection on CPU or rewrite it.
		// The first window must obtain an explicit CPU confirmation.
		s.Device, s.configured = "cpu", false
		return nil
	}
	s.Device, s.configured = settings.Device, true
	return nil
}

func (s *Supervisor) SelectDevice(device string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if device != "cpu" && device != "cuda" {
		return fmt.Errorf("Выбери процессор или NVIDIA.")
	}
	if s.closed || s.active != nil && !s.active.Done {
		return fmt.Errorf("Сначала останови текущую задачу.")
	}
	if device == "cuda" && !s.cudaEnabled {
		return fmt.Errorf("Эта сборка работает только на процессоре. Для NVIDIA нужна полная сборка.")
	}
	if device == "cuda" && !s.hardware.CUDAAvailable {
		return fmt.Errorf("%s", s.hardware.GPUReason)
	}
	bytes, _ := json.MarshalIndent(desktopSettings{Schema: "zvukbook-device-v1", Device: device}, "", "  ")
	path := filepath.Join(s.Data, "desktop-settings.json")
	if err := os.WriteFile(path+".part", bytes, 0600); err != nil {
		return err
	}
	if err := os.Rename(path+".part", path); err != nil {
		return err
	}
	s.Device, s.configured = device, true
	return nil
}

func (s *Supervisor) handleSettings(w http.ResponseWriter, r *http.Request) {
	var request struct {
		Device string `json:"device,omitempty"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&request); err != nil {
		http.Error(w, "invalid settings", 400)
		return
	}
	if r.URL.Path == "/settings/set" {
		if err := s.SelectDevice(request.Device); err != nil {
			_ = json.NewEncoder(w).Encode(map[string]any{"error": err.Error()})
			return
		}
	} else if r.URL.Path != "/settings/get" {
		http.NotFound(w, r)
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	ceiling := uint64(2 * winhostGiB)
	if s.Device == "cuda" {
		ceiling = 6 * winhostGiB
	}
	recommended := "cpu"
	if s.hardware.CUDAAvailable {
		if memory, err := winhost.MemoryNow(); err == nil {
			if _, err := planResources(memory, "cuda", "render"); err == nil {
				recommended = "cuda"
			}
		}
	}
	availableDevices := []string{"cpu"}
	if s.cudaEnabled {
		availableDevices = append(availableDevices, "cuda")
	}
	_ = json.NewEncoder(w).Encode(map[string]any{"device": s.Device, "configured": s.configured && (s.Device != "cuda" || s.hardware.CUDAAvailable),
		"hardware": s.hardware, "worker_memory_ceiling_bytes": ceiling, "cpu_percent": 25, "recommended_device": recommended, "available_devices": availableDevices})
}

const winhostGiB uint64 = 1024 * 1024 * 1024

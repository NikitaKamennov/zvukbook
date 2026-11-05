//go:build windows && amd64

package supervisor

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"sync"
	"time"

	"zvukbook/desktop/internal/winhost"
)

type Supervisor struct {
	Bundle, Data, Device, Token, URL string
	mu                               sync.Mutex
	active                           *task
	closed                           bool
	configured                       bool
	cudaEnabled                      bool
	hardware                         Hardware
	server                           *http.Server
	listener                         net.Listener
}

type task struct {
	ID             string
	PID            uint32
	Done           bool
	Exit           uint32
	Reason         string
	ResourcePaused bool
	cancel         chan struct{}
	once           sync.Once
}

var identity = regexp.MustCompile(`^[a-f0-9]{32}$`)

func New(bundle, data, device string, availableDevices []string) (*Supervisor, error) {
	if device != "cpu" && device != "cuda" {
		return nil, fmt.Errorf("device must be cpu or cuda")
	}
	if availableDevices == nil {
		availableDevices = []string{"cpu", "cuda"} // Older full bundles.
	}
	devices := map[string]bool{}
	for _, supported := range availableDevices {
		if (supported != "cpu" && supported != "cuda") || devices[supported] {
			return nil, fmt.Errorf("Некорректный список устройств в комплекте приложения.")
		}
		devices[supported] = true
		info, err := os.Stat(filepath.Join(bundle, "runtime", supported, "python.exe"))
		if err != nil || !info.Mode().IsRegular() {
			return nil, fmt.Errorf("В комплекте отсутствует runtime %s. Пересобери или переустанови приложение.", supported)
		}
	}
	if !devices["cpu"] {
		return nil, fmt.Errorf("В комплекте обязательно должен быть процессорный runtime.")
	}
	if !devices[device] {
		return nil, fmt.Errorf("Эта сборка работает только на процессоре. Для NVIDIA нужна полная сборка.")
	}
	var random [32]byte
	if _, err := rand.Read(random[:]); err != nil {
		return nil, err
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	s := &Supervisor{Bundle: bundle, Data: data, Device: device, cudaEnabled: devices["cuda"], hardware: detectHardware(devices["cuda"]), Token: hex.EncodeToString(random[:]), URL: "http://" + listener.Addr().String(), listener: listener}
	if err := s.loadSettings(); err != nil {
		listener.Close()
		return nil, err
	}
	s.server = &http.Server{Handler: http.HandlerFunc(s.handle), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 90 * time.Second}
	go s.server.Serve(listener)
	return s, nil
}

func (s *Supervisor) Environment(output string, worker bool, device string) []string {
	systemRoot := os.Getenv("SystemRoot")
	profile := os.Getenv("USERPROFILE")
	local := os.Getenv("LOCALAPPDATA")
	roaming := os.Getenv("APPDATA")
	if worker {
		profile = filepath.Join(output, "temp")
		local = profile
		roaming = profile
	}
	env := []string{"SystemRoot=" + systemRoot, "WINDIR=" + systemRoot, "PATH=" + filepath.Join(systemRoot, "System32"),
		"USERPROFILE=" + profile, "LOCALAPPDATA=" + local, "APPDATA=" + roaming, "TEMP=" + filepath.Join(output, "temp"),
		"TMP=" + filepath.Join(output, "temp"), "PYTHONUTF8=1", "ZVUKBOOK_BUNDLE_ROOT=" + s.Bundle, "ZVUKBOOK_DEVICE=" + device,
		"OMP_NUM_THREADS=2", "OMP_THREAD_LIMIT=2", "OPENBLAS_NUM_THREADS=2", "MKL_NUM_THREADS=2", "NUMEXPR_NUM_THREADS=2",
		"TESSDATA_PREFIX=" + filepath.Join(s.Bundle, "tools", "tesseract", "tessdata")}
	if !worker {
		env = append(env, "ZVUKBOOK_DATA_ROOT="+s.Data, "ZVUKBOOK_SUPERVISOR_URL="+s.URL, "ZVUKBOOK_SUPERVISOR_TOKEN="+s.Token)
	}
	return env
}

func (s *Supervisor) handle(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	expected := "Bearer " + s.Token
	if r.Method != "POST" || subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte(expected)) != 1 {
		http.Error(w, "forbidden", http.StatusForbidden)
		return
	}
	if r.URL.Path == "/settings/get" || r.URL.Path == "/settings/set" {
		s.handleSettings(w, r)
		return
	}
	var request struct {
		ID   string `json:"job_id"`
		Kind string `json:"kind,omitempty"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 32768))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&request); err != nil || !identity.MatchString(request.ID) {
		http.Error(w, "invalid request", 400)
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	var result any
	var err error
	switch r.URL.Path {
	case "/worker/start":
		if s.closed {
			err = fmt.Errorf("Приложение останавливается.")
			break
		}
		if s.active != nil && !s.active.Done {
			err = fmt.Errorf("Уже работает одна тяжёлая задача.")
			break
		}
		if request.Kind != "import" && request.Kind != "render" {
			err = fmt.Errorf("Неизвестная операция.")
			break
		}
		result, err = s.start(request.ID, request.Kind)
	case "/worker/state":
		if s.active == nil || s.active.ID != request.ID {
			err = fmt.Errorf("Supervisor не знает эту задачу.")
			break
		}
		result = map[string]any{"done": s.active.Done, "exit_code": s.active.Exit, "reason": s.active.Reason, "resource_paused": s.active.ResourcePaused}
	case "/worker/cancel":
		if s.active == nil || s.active.ID != request.ID {
			err = fmt.Errorf("Supervisor не знает эту задачу.")
			break
		}
		s.active.once.Do(func() { close(s.active.cancel) })
		result = map[string]any{"ok": true}
	default:
		http.NotFound(w, r)
		return
	}
	if err != nil {
		result = map[string]any{"error": err.Error()}
		if _, ok := err.(resourceError); ok {
			result = map[string]any{"error": err.Error(), "resource_paused": true}
		}
	}
	_ = json.NewEncoder(w).Encode(result)
}

func canonicalInside(root, path string) error {
	actual, err := filepath.EvalSymlinks(path)
	if err != nil {
		return err
	}
	relative, err := filepath.Rel(root, actual)
	if err != nil || relative == ".." || filepath.IsAbs(relative) || len(relative) > 3 && relative[:3] == "..\\" {
		return fmt.Errorf("Путь задачи вышел за выделенный каталог.")
	}
	return nil
}

func (s *Supervisor) start(id, kind string) (any, error) {
	jobs := filepath.Join(s.Data, "library", "jobs")
	directory := filepath.Join(jobs, id)
	inputs := filepath.Join(directory, "input")
	output := filepath.Join(directory, "out")
	for _, path := range []string{directory, inputs, filepath.Join(inputs, "job.json"), output} {
		if err := canonicalInside(jobs, path); err != nil {
			return nil, err
		}
	}
	logHost(directory, "worker_launch_preparing", map[string]any{"job_id": id, "kind": kind})
	configInfo, err := os.Stat(filepath.Join(inputs, "job.json"))
	if err != nil {
		return nil, err
	}
	if configInfo.Size() > 1024*1024 {
		return nil, fmt.Errorf("Слишком большой конфиг задачи.")
	}
	memory, err := winhost.MemoryNow()
	if err != nil {
		return nil, err
	}
	device := "cpu"
	if kind == "render" {
		if !s.configured {
			return nil, fmt.Errorf("Сначала выбери устройство озвучки в настройках приложения.")
		}
		var config struct {
			Settings struct {
				Device string `json:"device"`
			} `json:"settings"`
		}
		bytes, readErr := os.ReadFile(filepath.Join(inputs, "job.json"))
		if readErr != nil || json.Unmarshal(bytes, &config) != nil {
			return nil, fmt.Errorf("Не удалось прочитать профиль задачи.")
		}
		device = config.Settings.Device
		if device != s.Device {
			return nil, fmt.Errorf("Профиль задачи изменился. Выбери главы и запусти озвучку заново.")
		}
		if device == "cuda" && !s.hardware.CUDAAvailable {
			return nil, fmt.Errorf("%s", s.hardware.GPUReason)
		}
	}
	plan, err := planResources(memory, device, kind)
	if err != nil {
		return nil, err
	}
	budget, ramReserve, commitReserve := plan.Budget, plan.RAMReserve, plan.CommitReserve
	logHost(directory, "resource_budget_selected", map[string]any{"device": device, "commit_limit_bytes": budget,
		"ram_reserve_bytes": ramReserve, "commit_reserve_bytes": commitReserve, "cpu_threads": plan.Threads})
	free, err := winhost.DiskFree(output)
	if err != nil || free < 5*winhost.GiB+256*1024*1024 {
		return nil, resourceFailure("Недостаточно места с резервом 5 GiB.")
	}
	if err = os.MkdirAll(filepath.Join(output, "temp"), 0700); err != nil {
		return nil, err
	}
	_ = os.Remove(filepath.Join(output, "cancel.request"))
	privatePython := filepath.Join(s.Bundle, "runtime", device)
	var nonce [12]byte
	if _, err = rand.Read(nonce[:]); err != nil {
		return nil, err
	}
	logHost(directory, "appcontainer_preparing", nil)
	container, err := winhost.NewContainer("Zvukbook.Worker."+hex.EncodeToString(nonce[:]),
		[]string{privatePython, filepath.Join(s.Bundle, "app"), filepath.Join(s.Bundle, "models"), filepath.Join(s.Bundle, "tools"), inputs}, output)
	if err != nil {
		return nil, err
	}
	logHost(directory, "appcontainer_ready", nil)
	environment := s.Environment(output, true, device)
	for _, name := range []string{"OMP_NUM_THREADS", "OMP_THREAD_LIMIT", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"} {
		for i, value := range environment {
			if len(value) > len(name) && value[:len(name)+1] == name+"=" {
				environment[i] = name + "=" + strconv.Itoa(plan.Threads)
			}
		}
	}
	environment = append(environment, "ZVUKBOOK_CPU_THREADS="+strconv.Itoa(plan.Threads), "ZVUKBOOK_FRAGMENT_CHARACTERS="+strconv.Itoa(plan.FragmentCharacters))
	if plan.LowMemory {
		environment = append(environment, "ZVUKBOOK_LOW_MEMORY=1")
	}
	environment = append(environment, "ZVUKBOOK_WORKER_INPUT="+inputs, "ZVUKBOOK_WORKER_OUTPUT="+output)
	process, err := winhost.Start(filepath.Join(privatePython, "python.exe"), []string{"-I", "-B", "-m", "book_renderer.worker", kind}, environment, output, budget, container)
	if err != nil {
		container.Close()
		return nil, err
	}
	logHost(directory, "process_created_suspended", map[string]any{"pid": process.PID()})
	launch := map[string]any{"schema": "zvukbook-windows-worker-v1", "worker_pid": process.PID(), "job_id": id,
		"job_verified_before_resume": true, "isolation": "appcontainer-no-network", "cpu_hard_cap_percent": 25,
		"job_commit_limit_bytes": budget, "ram_reserve_bytes": ramReserve, "commit_reserve_bytes": commitReserve,
		"logical_cpus": runtime.NumCPU(), "device": device}
	launch["kind"], launch["cpu_threads"], launch["fragment_characters"], launch["low_memory"] = kind, plan.Threads, plan.FragmentCharacters, plan.LowMemory
	bytes, _ := json.MarshalIndent(launch, "", "  ")
	if err = os.WriteFile(filepath.Join(inputs, "launch.json"), bytes, 0600); err == nil {
		err = process.Resume()
	}
	if err != nil {
		process.Close()
		container.Close()
		return nil, err
	}
	logHost(directory, "process_resumed", map[string]any{"pid": process.PID()})
	current := &task{ID: id, PID: process.PID(), cancel: make(chan struct{})}
	s.active = current
	go s.monitor(current, process, container, directory, output, ramReserve, commitReserve, launch)
	return map[string]any{"pid": current.PID}, nil
}

func (s *Supervisor) monitor(current *task, process *winhost.Process, container *winhost.Container, directory, output string, ramReserve, commitReserve uint64, launch map[string]any) {
	started := time.Now()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	var reason string
	var resourcePaused bool
	var cancelTime time.Time
	exit := uint32(1)
	for {
		done, code, err := process.Poll()
		if err != nil {
			reason = err.Error()
			break
		}
		if done {
			exit = code
			logHost(directory, "process_exited", map[string]any{"exit_code": code})
			break
		}
		if !cancelTime.IsZero() && time.Since(cancelTime) >= 10*time.Second {
			reason = "Истекло ожидание остановки worker."
			break
		}
		memory, err := winhost.MemoryNow()
		free, diskErr := winhost.DiskFree(output)
		if reason == "" && (err != nil || diskErr != nil || memory.RAMAvailable < ramReserve || memory.CommitAvailable < commitReserve || free < 5*winhost.GiB) {
			reason = "Исчерпан резерв RAM/commit/диска."
			resourcePaused = true
		}
		if reason == "" && time.Since(started) > 6*time.Hour {
			reason = "Превышено время задачи."
			resourcePaused = true
		}
		if reason == "" {
			if err := scratchBudget(output); err != nil {
				reason = err.Error()
				resourcePaused = true
			}
		}
		if reason != "" && cancelTime.IsZero() {
			logHost(directory, "stop_requested", map[string]any{"reason": reason, "resource_paused": resourcePaused})
			cancelTime = time.Now()
			_ = os.WriteFile(filepath.Join(output, "cancel.request"), []byte(reason), 0600)
		}
		select {
		case <-current.cancel:
			if cancelTime.IsZero() {
				logHost(directory, "user_cancel_requested", nil)
				reason = "Задача остановлена."
				cancelTime = time.Now()
				_ = os.WriteFile(filepath.Join(output, "cancel.request"), []byte(reason), 0600)
			}
			<-ticker.C
		case <-ticker.C:
		}
	}
	stats, statsErr := process.Stats()
	// Job accounting can lag the process signal briefly, even without children.
	until := time.Now().Add(2 * time.Second)
	for statsErr == nil && stats["active_processes"].(uint32) > 0 && reason == "" && time.Now().Before(until) {
		time.Sleep(50 * time.Millisecond)
		stats, statsErr = process.Stats()
	}
	if statsErr != nil && reason == "" {
		reason = statsErr.Error()
	}
	if statsErr == nil && stats["active_processes"].(uint32) > 0 && reason == "" {
		reason = "Worker оставил дочерние процессы."
	}
	logHost(directory, "isolation_cleanup_started", nil)
	process.Close()
	container.Close()
	logHost(directory, "isolation_cleanup_finished", nil)
	if reason != "" {
		exit = 1
	}
	if reason == "" && exit != 0 {
		reason = fmt.Sprintf("Обработчик завершился с кодом %d (0x%08X). Подробности в worker.log.", exit, exit)
	}
	launch["exit_code"], launch["reason"], launch["stats"], launch["elapsed_seconds"] = exit, reason, stats, time.Since(started).Seconds()
	launch["resource_paused"] = resourcePaused
	// Keep the end of a long log, including the actual failure, instead of
	// discarding logs larger than 1 MiB before the backend removes scratch.
	if err := captureWorkerLog(output, directory, launch); err != nil {
		launch["log_capture_error"] = err.Error()
	}
	bytes, _ := json.MarshalIndent(launch, "", "  ")
	_ = os.WriteFile(filepath.Join(directory, "host-report.json"), bytes, 0600)
	logHost(directory, "worker_finalized", map[string]any{"exit_code": exit, "reason": reason, "stats": stats})
	s.mu.Lock()
	current.Done = true
	current.Exit = exit
	current.Reason = reason
	current.ResourcePaused = resourcePaused
	s.mu.Unlock()
}

func logHost(directory, event string, fields map[string]any) {
	entry := map[string]any{"time": time.Now().UTC().Format(time.RFC3339Nano), "event": event}
	for name, value := range fields {
		entry[name] = value
	}
	log, err := os.OpenFile(filepath.Join(directory, "host.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0600)
	if err != nil {
		return
	}
	defer log.Close()
	_ = json.NewEncoder(log).Encode(entry)
}

func captureWorkerLog(output, directory string, report map[string]any) error {
	const maximum int64 = 2 * 1024 * 1024
	log, err := os.Open(filepath.Join(output, "worker.log"))
	if err != nil {
		return err
	}
	defer log.Close()
	info, err := log.Stat()
	if err != nil {
		return err
	}
	report["worker_log_bytes"] = info.Size()
	report["worker_log_truncated"] = info.Size() > maximum
	if info.Size() > maximum {
		if _, err := log.Seek(info.Size()-maximum, io.SeekStart); err != nil {
			return err
		}
	}
	tail, err := io.ReadAll(io.LimitReader(log, maximum))
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(directory, "worker.log"), tail, 0600)
}

func scratchBudget(root string) error {
	var total int64
	entries := 0
	return filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if os.IsNotExist(walkErr) {
			return nil
		}
		if walkErr != nil {
			return fmt.Errorf("Не удалось проверить временные файлы: %w", walkErr)
		}
		entries++
		if entries > 10000 || entry.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("Превышен предел временных файлов или обнаружена ссылка.")
		}
		if entry.IsDir() {
			return nil
		}
		info, err := entry.Info()
		if os.IsNotExist(err) {
			return nil
		}
		if err != nil {
			return err
		}
		total += info.Size()
		if info.Size() > 1<<30 || total > 2<<30 {
			return fmt.Errorf("Временный файл превысил 1 GiB или каталог задачи — 2 GiB.")
		}
		return nil
	})
}

func (s *Supervisor) Close() {
	s.mu.Lock()
	s.closed = true
	if s.active != nil && !s.active.Done {
		s.active.once.Do(func() { close(s.active.cancel) })
	}
	s.mu.Unlock()
	deadline := time.Now().Add(15 * time.Second)
	for time.Now().Before(deadline) {
		s.mu.Lock()
		done := s.active == nil || s.active.Done
		s.mu.Unlock()
		if done {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	s.server.Close()
}

func (s *Supervisor) BackendEnvironment() []string {
	return s.Environment(filepath.Join(s.Data, "backend"), false, s.Device)
}

func Port(value int) string { return strconv.Itoa(value) }

//go:build windows && amd64

package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"github.com/wailsapp/wails/v2"
	"github.com/wailsapp/wails/v2/pkg/options"
	"github.com/wailsapp/wails/v2/pkg/options/assetserver"
	"github.com/wailsapp/wails/v2/pkg/options/windows"
	wruntime "github.com/wailsapp/wails/v2/pkg/runtime"

	"zvukbook/desktop/internal/supervisor"
	"zvukbook/desktop/internal/winhost"
)

type Desktop struct {
	context context.Context
	data    string
	smoke   bool
	backend string
	token   string
}

var bookIdentity = regexp.MustCompile(`^[a-f0-9]{32}$`)

func (d *Desktop) SaveAudio(book string, chapter int) (bool, error) {
	if !bookIdentity.MatchString(book) || chapter < 0 || chapter > 2000 {
		return false, fmt.Errorf("Неверная глава.")
	}
	return d.save("/api/books/"+book+fmt.Sprintf("/audio/%d", chapter), fmt.Sprintf("%04d.mp3", chapter), "*.mp3")
}

func (d *Desktop) SaveArchive(book string) (bool, error) {
	if !bookIdentity.MatchString(book) {
		return false, fmt.Errorf("Неверная книга.")
	}
	return d.save("/api/books/"+book+"/export", "Звуккниг.zip", "*.zip")
}

func (d *Desktop) save(endpoint, filename, pattern string) (bool, error) {
	destination, err := wruntime.SaveFileDialog(d.context, wruntime.SaveDialogOptions{
		Title: "Сохранить файл", DefaultFilename: filename,
		Filters: []wruntime.FileFilter{{DisplayName: pattern, Pattern: pattern}},
	})
	if err != nil || destination == "" {
		return false, err
	}
	request, err := http.NewRequest("GET", d.backend+endpoint, nil)
	if err != nil {
		return false, err
	}
	request.Header.Set("X-Zvukbook-Token", d.token)
	client := &http.Client{Transport: &http.Transport{Proxy: nil}, Timeout: 30 * time.Minute}
	defer client.CloseIdleConnections()
	response, err := client.Do(request)
	if err != nil {
		return false, err
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		return false, fmt.Errorf("Не удалось получить файл (HTTP %d). Дождись завершения задачи.", response.StatusCode)
	}
	output, err := os.CreateTemp(filepath.Dir(destination), ".zvukbook-save-*.part")
	if err != nil {
		return false, err
	}
	defer os.Remove(output.Name())
	_, copyErr := io.Copy(output, response.Body)
	closeErr := output.Close()
	if copyErr != nil {
		return false, copyErr
	}
	if closeErr != nil {
		return false, closeErr
	}
	if err := os.Rename(output.Name(), destination); err != nil {
		return false, err
	}
	return true, nil
}

func (d *Desktop) SmokeResult(payload string) {
	if !d.smoke || len(payload) > 32768 {
		return
	}
	_ = os.WriteFile(filepath.Join(d.data, "ui-smoke.json"), []byte(payload), 0600)
	wruntime.Quit(d.context)
}

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		for _, arg := range os.Args {
			if arg == "--smoke" || arg == "--headless" {
				os.Exit(1)
			}
		}
		message, _ := syscall.UTF16PtrFromString(err.Error())
		title, _ := syscall.UTF16PtrFromString("Звуккниг — ошибка запуска")
		syscall.NewLazyDLL("user32.dll").NewProc("MessageBoxW").Call(0, uintptr(unsafe.Pointer(message)), uintptr(unsafe.Pointer(title)), 0x10)
		os.Exit(1)
	}
}

func run() error {
	executable, err := os.Executable()
	if err != nil {
		return err
	}
	bundleFlag := flag.String("bundle", filepath.Dir(executable), "bundle root")
	dataFlag := flag.String("data-root", filepath.Join(os.Getenv("LOCALAPPDATA"), "Zvukbook"), "separate Windows data directory")
	deviceFlag := flag.String("device", "", "explicit cpu or cuda override")
	headless := flag.Bool("headless", false, "authenticated local acceptance mode")
	smoke := flag.Bool("smoke", false, "open a hidden window, verify API, then exit")
	flag.Parse()
	if !*headless && !*smoke {
		release, existing, err := winhost.DesktopMutex()
		if err != nil {
			return err
		}
		defer release()
		if existing {
			focusWindow()
			return nil
		}
	}
	bundle, err := filepath.Abs(*bundleFlag)
	if err != nil {
		return err
	}
	bundle, err = filepath.EvalSymlinks(bundle)
	if err != nil {
		return err
	}
	data, err := filepath.Abs(*dataFlag)
	if err != nil {
		return err
	}
	if actual, resolveErr := filepath.EvalSymlinks(data); resolveErr == nil {
		data = actual
	} else {
		parent, parentErr := filepath.EvalSymlinks(filepath.Dir(data))
		if parentErr != nil {
			return parentErr
		}
		data = filepath.Join(parent, filepath.Base(data))
	}
	lowered := strings.ToLower(data) + "\\"
	if strings.Contains(lowered, "\\.local-data\\books-renderer\\") {
		return fmt.Errorf("Linux-каталог данных нельзя открывать из Windows.")
	}
	if rel, err := filepath.Rel(bundle, data); err == nil && rel != ".." && !strings.HasPrefix(rel, "..\\") {
		return fmt.Errorf("Книги должны храниться вне папки приложения.")
	}
	if err = os.MkdirAll(filepath.Join(data, "backend", "temp"), 0700); err != nil {
		return err
	}
	data, err = filepath.EvalSymlinks(data)
	if err != nil {
		return err
	}
	unlock, err := winhost.ExclusiveFile(filepath.Join(data, "desktop.lock"))
	if err != nil {
		focusWindow()
		if *headless || *smoke {
			return fmt.Errorf("Другой экземпляр уже открыл Windows-библиотеку.")
		}
		return nil
	}
	defer unlock()
	if err = winhost.PrivateDirectory(data); err != nil {
		return fmt.Errorf("Не удалось защитить Windows-каталог данных: %w", err)
	}
	configBytes, err := os.ReadFile(filepath.Join(bundle, "config", "windows-desktop.json"))
	if err != nil {
		return err
	}
	var config struct {
		Device           string   `json:"device"`
		AvailableDevices []string `json:"available_devices"`
	}
	if err = json.Unmarshal(configBytes, &config); err != nil {
		return err
	}
	if *deviceFlag != "" {
		config.Device = *deviceFlag
	}
	service, err := supervisor.New(bundle, data, config.Device, config.AvailableDevices)
	if err != nil {
		return err
	}
	defer service.Close()
	if *deviceFlag != "" {
		if err := service.SelectDevice(*deviceFlag); err != nil {
			return err
		}
	}
	browserPath, err := winhost.PrepareBrowser(bundle)
	if err != nil {
		return err
	}
	if err = os.Setenv("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", browserPath); err != nil {
		return err
	}
	_ = os.Remove(filepath.Join(data, "desktop-stop.request"))
	_ = os.Remove(filepath.Join(data, "headless-ready.json"))
	defer os.Remove(filepath.Join(data, "headless-ready.json"))
	_ = os.Remove(filepath.Join(data, "backend-stop.request"))
	_ = os.Remove(filepath.Join(data, "backend-ready.json"))
	backend, err := winhost.Start(filepath.Join(bundle, "runtime", "cpu", "python.exe"),
		[]string{"-I", "-B", "-m", "book_renderer.windows_server"}, service.BackendEnvironment(), filepath.Join(data, "backend"), 2*winhost.GiB, nil)
	if err != nil {
		return err
	}
	defer backend.Close()
	if err = backend.Resume(); err != nil {
		return err
	}
	defer func() {
		_ = os.WriteFile(filepath.Join(data, "backend-stop.request"), []byte("close"), 0600)
		until := time.Now().Add(15 * time.Second)
		for time.Now().Before(until) {
			done, _, _ := backend.Poll()
			if done {
				break
			}
			time.Sleep(100 * time.Millisecond)
		}
	}()
	var ready struct {
		PID  uint32 `json:"pid"`
		Port int    `json:"port"`
	}
	deadline := time.Now().Add(45 * time.Second)
	for {
		bytes, readErr := os.ReadFile(filepath.Join(data, "backend-ready.json"))
		if readErr == nil && json.Unmarshal(bytes, &ready) == nil && ready.PID == backend.PID() && ready.Port > 0 {
			break
		}
		done, code, pollErr := backend.Poll()
		if pollErr != nil {
			return pollErr
		}
		if done {
			return fmt.Errorf("Python backend завершился с кодом %d. Журнал: %s", code, filepath.Join(data, "backend", "worker.log"))
		}
		if time.Now().After(deadline) {
			return fmt.Errorf("Backend не подтвердил готовность за 45 секунд.")
		}
		time.Sleep(100 * time.Millisecond)
	}
	target, _ := url.Parse("http://127.0.0.1:" + supervisor.Port(ready.Port))
	proxy := httputil.NewSingleHostReverseProxy(target)
	proxy.Transport = &http.Transport{Proxy: nil, ResponseHeaderTimeout: 30 * time.Second}
	originalDirector := proxy.Director
	proxy.Director = func(r *http.Request) {
		originalDirector(r)
		r.Host = target.Host
		r.Header.Set("X-Zvukbook-Token", service.Token)
		if r.Header.Get("Origin") != "" {
			r.Header.Set("Origin", target.String())
		}
	}
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")
		if origin != "" && origin != "http://wails.localhost" {
			http.Error(w, "forbidden origin", 403)
			return
		}
		if r.Header.Get("Sec-Fetch-Site") == "cross-site" {
			http.Error(w, "cross-site request", 403)
			return
		}
		if r.URL.Path == "/index.html" {
			r.URL.Path = "/"
		}
		proxy.ServeHTTP(w, r)
	})
	if *headless {
		listener, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			return err
		}
		server := &http.Server{ReadHeaderTimeout: 5 * time.Second, Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Header.Get("X-Zvukbook-UI-Key") != service.Token {
				http.Error(w, "forbidden", 403)
				return
			}
			r.Header.Set("Origin", "http://wails.localhost")
			handler.ServeHTTP(w, r)
		})}
		defer server.Close()
		go server.Serve(listener)
		bytes, _ := json.Marshal(map[string]any{"url": "http://" + listener.Addr().String(), "token": service.Token, "pid": os.Getpid()})
		if err = os.WriteFile(filepath.Join(data, "headless-ready.json"), bytes, 0600); err != nil {
			return err
		}
		signals := make(chan os.Signal, 1)
		signal.Notify(signals, os.Interrupt, syscall.SIGTERM)
		defer signal.Stop(signals)
		for {
			select {
			case <-signals:
				return nil
			case <-time.After(200 * time.Millisecond):
			}
			if _, err := os.Stat(filepath.Join(data, "desktop-stop.request")); err == nil {
				return nil
			}
			if done, code, _ := backend.Poll(); done {
				return fmt.Errorf("Backend exit %d", code)
			}
		}
	}
	desktop := &Desktop{data: data, smoke: *smoke, backend: target.String(), token: service.Token}
	var smokeTimer *time.Timer
	bindings := []interface{}{desktop}
	if *smoke {
		_ = os.Remove(filepath.Join(data, "ui-smoke.json"))
	}
	err = wails.Run(&options.App{Title: "Звуккниг", Width: 1280, Height: 900, MinWidth: 900, MinHeight: 650, StartHidden: *smoke,
		AssetServer: &assetserver.Options{Handler: handler}, Bind: bindings,
		Windows: &windows.Options{WindowClassName: "Zvukbook", WebviewUserDataPath: filepath.Join(data, "webview"), WebviewBrowserPath: browserPath},
		OnStartup: func(ctx context.Context) {
			desktop.context = ctx
			if *smoke {
				smokeTimer = time.AfterFunc(60*time.Second, func() { wruntime.Quit(ctx) })
			}
		},
		OnShutdown: func(ctx context.Context) {
			if smokeTimer != nil {
				smokeTimer.Stop()
			}
		},
		OnDomReady: func(ctx context.Context) {
			if *smoke {
				wruntime.WindowExecJS(ctx, `(async()=>{
				const s=await fetch('/api/state').then(r=>r.json());
				if(!s.csrf_token||!document.getElementById('upload-form')||!window.go.main.Desktop.SaveAudio)throw Error('UI/API/bindings missing');
				const form=new FormData();form.append('file',new File(['Глава 1\n\nЭто вымышленный текст для проверки окна Windows. Птицы пели в саду, а над рекой светило солнце.'], 'ui-synthetic.txt'));
				const response=await fetch('/api/books/upload',{method:'POST',headers:{'X-CSRF-Token':s.csrf_token},body:form});
				if(!response.ok)throw Error('upload HTTP '+response.status);
				const result=await response.json();let complete=!!result.duplicate;
				for(let i=0;!complete&&i<45;i++){await new Promise(r=>setTimeout(r,1000));const state=await fetch('/api/state').then(r=>r.json());const job=state.jobs.find(j=>j.id===result.job_id);if(job?.status==='completed')complete=true;else if(job&&!['queued','running'].includes(job.status))throw Error(job.error||job.status);}
				if(!complete)throw Error('native import timeout');
				await window.go.main.Desktop.SmokeResult(JSON.stringify({ok:true,title:document.title,upload:true,native_import:true,save_bindings:true,device:s.limits.device}));
				})().catch(e=>window.go.main.Desktop.SmokeResult(JSON.stringify({ok:false,error:String(e)})))`)
			}
		},
	})
	if err != nil {
		return err
	}
	if *smoke {
		bytes, err := os.ReadFile(filepath.Join(data, "ui-smoke.json"))
		if err != nil {
			return fmt.Errorf("Окно не подтвердило UI smoke: %w", err)
		}
		var result struct {
			OK bool `json:"ok"`
		}
		if json.Unmarshal(bytes, &result) != nil || !result.OK {
			return fmt.Errorf("UI smoke failed: %s", bytes)
		}
	}
	return nil
}

func focusWindow() {
	user32 := syscall.NewLazyDLL("user32.dll")
	class, _ := syscall.UTF16PtrFromString("Zvukbook")
	window, _, _ := user32.NewProc("FindWindowW").Call(uintptr(unsafe.Pointer(class)), 0)
	if window != 0 {
		user32.NewProc("ShowWindow").Call(window, 9)
		user32.NewProc("SetForegroundWindow").Call(window)
	}
}

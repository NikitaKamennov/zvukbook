//go:build windows && amd64

package supervisor

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"syscall"
	"time"
	"unsafe"
	"zvukbook/desktop/internal/winhost"
)

type Hardware struct {
	LogicalCPUs   int    `json:"logical_cpus"`
	RAMTotal      uint64 `json:"ram_total_bytes"`
	GPUName       string `json:"gpu_name"`
	CUDADriver    int32  `json:"cuda_driver"`
	CUDAAvailable bool   `json:"cuda_available"`
	GPUReason     string `json:"gpu_reason"`
}

func detectHardware(cudaEnabled bool) Hardware {
	h := Hardware{LogicalCPUs: runtime.NumCPU(), GPUReason: "Совместимая NVIDIA не обнаружена. Можно озвучивать на процессоре."}
	if memory, err := winhost.MemoryNow(); err == nil {
		h.RAMTotal = memory.RAMTotal
	}
	if !cudaEnabled {
		h.GPUReason = "Облегчённая CPU-сборка работает на процессоре. Для ускорения NVIDIA нужна полная сборка."
		return h
	}
	completed := make(chan Hardware, 1)
	go func(snapshot Hardware) { completed <- probeGPU(snapshot) }(h)
	select {
	case result := <-completed:
		return result
	case <-time.After(4 * time.Second):
		h.GPUReason = "NVIDIA не ответила за отведённое время. Выбери процессор."
		return h
	}
}

func probeGPU(h Hardware) Hardware {
	driver := syscall.NewLazyDLL(filepath.Join(os.Getenv("SystemRoot"), "System32", "nvcuda.dll"))
	if err := driver.Load(); err != nil {
		return h
	}
	var version, count int32
	if !cudaCall(driver, "cuInit", 0) || !cudaCall(driver, "cuDriverGetVersion", uintptr(unsafe.Pointer(&version))) ||
		!cudaCall(driver, "cuDeviceGetCount", uintptr(unsafe.Pointer(&count))) || count < 1 {
		return h
	}
	h.CUDADriver = version
	var device, major, minor int32
	var name [256]byte
	if !cudaCall(driver, "cuDeviceGet", uintptr(unsafe.Pointer(&device)), 0) ||
		!cudaCall(driver, "cuDeviceGetName", uintptr(unsafe.Pointer(&name[0])), uintptr(len(name)), uintptr(device)) ||
		!cudaCall(driver, "cuDeviceComputeCapability", uintptr(unsafe.Pointer(&major)), uintptr(unsafe.Pointer(&minor)), uintptr(device)) {
		return h
	}
	for i, value := range name {
		if value == 0 {
			h.GPUName = string(name[:i])
			break
		}
	}
	if version < 12080 {
		h.GPUReason = "Для комплектного GPU-режима нужен более новый NVIDIA-драйвер. Процессор доступен без него."
	} else if major*10+minor < 75 {
		h.GPUReason = "Эта NVIDIA не входит в поддерживаемый набор GPU этого выпуска. Выбери процессор."
	} else {
		h.CUDAAvailable = true
		h.GPUReason = fmt.Sprintf("%s. Перед озвучкой дополнительно проверяется свободная видеопамять.", h.GPUName)
	}
	return h
}

//go:uintptrescapes
func cudaCall(driver *syscall.LazyDLL, name string, args ...uintptr) bool {
	proc := driver.NewProc(name)
	if proc.Find() != nil {
		return false
	}
	result, _, _ := proc.Call(args...)
	return result == 0
}

//go:build windows && amd64

package winhost

import (
	"fmt"
	"syscall"
	"unsafe"
)

func DesktopMutex() (func(), bool, error) {
	name, _ := syscall.UTF16PtrFromString(`Local\Zvukbook.Desktop`)
	handle, _, err := kernel.NewProc("CreateMutexW").Call(0, 0, uintptr(unsafe.Pointer(name)))
	if handle == 0 {
		return nil, false, fmt.Errorf("CreateMutexW: %w", err)
	}
	return func() { syscall.CloseHandle(syscall.Handle(handle)) }, err == syscall.ERROR_ALREADY_EXISTS, nil
}

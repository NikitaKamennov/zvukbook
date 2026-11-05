//go:build windows && amd64

package winhost

import (
	"os/user"
	"syscall"
	"unsafe"
)

func ExclusiveFile(path string) (func(), error) {
	name, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return nil, err
	}
	handle, err := syscall.CreateFile(name, syscall.GENERIC_READ|syscall.GENERIC_WRITE, 0, nil, syscall.OPEN_ALWAYS, syscall.FILE_ATTRIBUTE_NORMAL, 0)
	if err != nil {
		return nil, err
	}
	return func() { syscall.CloseHandle(handle) }, nil
}

func PrivateDirectory(path string) error {
	current, err := user.Current()
	if err != nil {
		return err
	}
	sddl, err := syscall.UTF16PtrFromString("D:P(A;OICI;FA;;;" + current.Uid + ")(A;OICI;FA;;;SY)")
	if err != nil {
		return err
	}
	var descriptor unsafe.Pointer
	if err = winOK(advapi.NewProc("ConvertStringSecurityDescriptorToSecurityDescriptorW"), uintptr(unsafe.Pointer(sddl)), 1, uintptr(unsafe.Pointer(&descriptor)), 0); err != nil {
		return err
	}
	defer kernel.NewProc("LocalFree").Call(uintptr(descriptor))
	var present, defaulted int32
	var dacl unsafe.Pointer
	if err = winOK(advapi.NewProc("GetSecurityDescriptorDacl"), uintptr(descriptor), uintptr(unsafe.Pointer(&present)), uintptr(unsafe.Pointer(&dacl)), uintptr(unsafe.Pointer(&defaulted))); err != nil {
		return err
	}
	name, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return err
	}
	result, _, _ := advapi.NewProc("SetNamedSecurityInfoW").Call(uintptr(unsafe.Pointer(name)), 1, 0x80000004, 0, 0, uintptr(dacl), 0)
	if result != 0 {
		return syscall.Errno(result)
	}
	return nil
}

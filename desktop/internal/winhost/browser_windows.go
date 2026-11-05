//go:build windows && amd64

package winhost

import (
	"fmt"
	"os"
	"path/filepath"
)

func PrepareBrowser(bundle string) (string, error) {
	browser := filepath.Join(bundle, "tools", "webview2")
	if _, err := os.Stat(filepath.Join(browser, "msedgewebview2.exe")); err != nil {
		return "", fmt.Errorf("Отсутствует комплектный WebView2. Распакуй весь архив или переустанови приложение: %w", err)
	}
	// Microsoft's required read permissions for Fixed Version sandbox processes.
	// They apply only to browser files, never to the user's library.
	if err := icacls(browser, "/grant", "*S-1-15-2-1:(OI)(CI)(RX)", "*S-1-15-2-2:(OI)(CI)(RX)"); err != nil {
		return "", fmt.Errorf("Не удалось подготовить окно. Установи приложение для текущего пользователя или распакуй архив в свою папку на локальном NTFS-диске: %w", err)
	}
	return browser, nil
}

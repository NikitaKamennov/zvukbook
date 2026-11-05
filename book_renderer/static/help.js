(() => {
  'use strict';
  const dialog = document.getElementById('help-dialog');
  document.getElementById('open-help').addEventListener('click', () => dialog.showModal());
  document.getElementById('help-close').addEventListener('click', () => dialog.close());
})();

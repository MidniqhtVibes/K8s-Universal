const textarea = document.querySelector('#yaml-editor');
if (textarea && window.CodeMirror) {
  const editor = CodeMirror.fromTextArea(textarea, {
    mode: 'yaml', lineNumbers: true, lineWrapping: false, indentUnit: 2,
    tabSize: 2, indentWithTabs: false, theme: 'cluster-builder',
    extraKeys: {'Ctrl-S': () => document.querySelector('#manifest-form').requestSubmit(), 'Cmd-S': () => document.querySelector('#manifest-form').requestSubmit()}
  });
  editor.setSize('100%', '620px');
  setTimeout(() => editor.refresh(), 50);
}


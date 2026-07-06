const container = document.querySelector('#terminal');
const clusterId = container.dataset.clusterId;
const statusDot = document.querySelector('#terminal-status');
const adminMode = document.querySelector('#admin-mode');
const terminal = new Terminal({cursorBlink: true, convertEol: true, fontSize: 14, fontFamily: 'ui-monospace, SFMono-Regular, Consolas, monospace', theme: {background: '#07101a', foreground: '#dce7f2', cursor: '#63d4b3'}});
const fitAddon = new FitAddon.FitAddon();
terminal.loadAddon(fitAddon); terminal.open(container); fitAddon.fit();
window.addEventListener('resize', () => fitAddon.fit());
let buffer = ''; let busy = true; const history = []; let historyIndex = 0;
const readOnlyVerbs = new Set(['api-resources','api-versions','auth','cluster-info','describe','diff','explain','get','logs','options','top','version','wait']);
const prompt = () => { busy = false; buffer = ''; terminal.write('\r\n\x1b[32mkubectl>\x1b[0m '); };
const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
const socket = new WebSocket(`${scheme}://${location.host}/ws/clusters/${clusterId}/kubectl`);
socket.onopen = () => { statusDot.classList.add('connected'); };
socket.onclose = () => { statusDot.classList.remove('connected'); terminal.write('\r\n\x1b[31mVerbindung geschlossen.\x1b[0m'); busy = true; };
socket.onmessage = event => {
  const message = JSON.parse(event.data);
  if (message.type === 'ready') { terminal.writeln(`Verbunden mit ${message.cluster}.`); terminal.writeln('Beispiel: get nodes -o wide'); prompt(); }
  if (message.type === 'output') terminal.write(message.data);
  if (message.type === 'error') { terminal.write(`\r\n\x1b[31m${message.message}\x1b[0m`); prompt(); }
  if (message.type === 'exit') { terminal.write(`\r\n\x1b[90m[Exit ${message.code}${message.interrupted ? ', abgebrochen' : ''}]\x1b[0m`); prompt(); }
};
terminal.onData(data => {
  if (data === '\u0003') { if (busy) socket.send(JSON.stringify({type: 'interrupt'})); else terminal.write('^C'); return; }
  if (busy) return;
  if (data === '\r') {
    const command = buffer.trim(); terminal.write('\r\n');
    if (!command) { prompt(); return; }
    const verb = command.replace(/^kubectl\s+/, '').trim().split(/\s+/)[0].toLowerCase();
    const mutating = !readOnlyVerbs.has(verb);
    if (mutating && !adminMode.checked) { terminal.write('\x1b[33mMutierender Befehl: zuerst den Administrationsmodus aktivieren.\x1b[0m'); prompt(); return; }
    const confirmMutation = mutating && window.confirm(`Mutierenden Befehl wirklich ausführen?\n\nkubectl ${command}`);
    if (mutating && !confirmMutation) { terminal.write('\x1b[33mAbgebrochen.\x1b[0m'); prompt(); return; }
    history.push(command); historyIndex = history.length; busy = true;
    socket.send(JSON.stringify({type: 'command', command, confirm_mutation: confirmMutation})); return;
  }
  if (data === '\u007f') { if (buffer.length) { buffer = buffer.slice(0, -1); terminal.write('\b \b'); } return; }
  if (data === '\u001b[A' && history.length) { while (buffer.length) { terminal.write('\b \b'); buffer = buffer.slice(0, -1); } historyIndex = Math.max(0, historyIndex - 1); buffer = history[historyIndex]; terminal.write(buffer); return; }
  if (data.length > 1 && !data.includes('\r') && !data.includes('\n') && !data.startsWith('\u001b')) { buffer += data; terminal.write(data); return; }
  if (data.length === 1 && data >= ' ') { buffer += data; terminal.write(data); }
});

"""Execute the real renderer, capturing HTML before DOM decoration."""
import subprocess


def test_cargo_transfer_renders_selection_without_editing_controls():
    subprocess.run(['node', '-e', r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync('static/app.js', 'utf8');
const render = source.slice(source.indexOf('function renderDoc()'), source.indexOf('\nasync function ', source.indexOf('function renderDoc()')));
const context = {
  doc: {status:'cargo_edit', lines:[{id:42,pn:'TEST',gtin:'04640638349704',quantity:5,labels:2,tnved:'8536699008'}],operations:[],artifacts:[],codes:[],atks:[],exports:[],history:[]},
  user:{admin:true}, isSupervisor:()=>false, documentOperationAlerts:()=>'',
  e:x=>String(x ?? ''), fmt:x=>String(x), date:()=>'', documentBadge:()=>'',
  statusLastChecked:null, archive:false, states:{}, kinds:{}, html:'',
  shell: html=>{context.html=html; throw 'captured';}
};
vm.createContext(context); vm.runInContext(render,context);
function capture(){try{vm.runInContext('renderDoc()',context)}catch(e){if(e!=='captured')throw e;}return context.html;}
let html=capture();
assert(html.includes('data-action="move-lines"'));
assert(html.includes('class="move-line" value="42"'));
assert(!html.includes('class="qty-input"'));
assert(!html.includes('data-action="save-lines"'));
assert(!html.includes('data-action="import"'));
context.user.admin=false; html=capture();
assert(!html.includes('class="move-line"'));
assert(!html.includes('data-action="move-lines"'));
context.user.admin=true; context.doc.status='cargo_breakup';html=capture();
assert(!html.includes('class="move-line"'));
'''], check=True)

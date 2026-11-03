import fs from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

const elements = new Map();
function element(key) {
  if (!elements.has(key)) elements.set(key, {
    style:{}, dataset:{}, classList:{add(){},remove(){},toggle(){}},
    clientWidth:500, clientHeight:400, naturalWidth:1280, naturalHeight:960,
    events:{}, addEventListener(name, fn){this.events[name]=fn;},
    setAttribute(name,value){this[name]=value;}, getAttribute(name){return this[name];},
    setPointerCapture(){}, querySelector(selector){return element(`${key} ${selector}`);},
  });
  return elements.get(key);
}
const panes = [element('ref'),element('current')];
const buttons = ['fit','1','2'].map(value=>{const item=element(value);item.dataset.inspectionZoom=value;return item;});
const dialog = element('dialog');
dialog.querySelector = element;
dialog.querySelectorAll = selector => selector === '.inspection-pane' ? panes : buttons;
const context = vm.createContext({ResizeObserver:class {observe(){}},console});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const Viewer = context.PhotoReviewViewer;
const plain = value => JSON.parse(JSON.stringify(value));
assert.deepEqual(plain(Viewer.layout(500,400,1280,960,'fit',{x:.5,y:.5})),
  {width:500,height:375,left:0,top:12.5});
assert.deepEqual(plain(Viewer.layout(500,400,1280,960,'1',{x:0,y:1})),
  {width:1280,height:960,left:0,top:-560});
assert.equal(Viewer.layout(0,0,0,0,'fit',{x:.5,y:.5}),null);
const items=[0,1,2].map(index=>({index,group_id:1,filename:`${index}.ARW`,preview_url:`${index}.jpg`}));
let viewer;
viewer = new Viewer(dialog, {selectPhoto:index=>viewer.setPhoto(items[index],items,false),
  ratings:item=>`rating:${item.index}`,score:item=>`score:${item.index}`});
viewer.setPhoto(items[0],items,false);
viewer.toggleCompare();
assert.equal(viewer.referenceIndex,0);
assert.equal(viewer.current.index,1);
assert.equal(element('#dialog-reference-ratings').innerHTML,'rating:0');
viewer.setZoom('1');
viewer.setPhoto(items[2],items,false);
assert.equal(viewer.referenceIndex,0);
assert.equal(viewer.zoom,'1');
const stage=element('current .inspection-stage');
stage.events.pointerdown({button:0,pointerId:1,clientX:100,clientY:100,target:{closest(){return null;}},preventDefault(){}});
stage.events.pointermove({pointerId:1,clientX:200,clientY:150});
assert.ok(viewer.center.x < .5 && viewer.center.y < .5);
assert.equal(element('ref img').style.left,element('current img').style.left);
viewer.pinCurrent();
assert.equal(viewer.referenceIndex,2);
assert.equal(viewer.current.index,1);
viewer.toggleCompare();
assert.equal(viewer.referenceIndex,null);
viewer.setZoom('fit');
assert.deepEqual(plain(viewer.center),{x:.5,y:.5});
viewer.setPhoto({...items[0],group_id:2},[{...items[0],group_id:2}],false);
assert.equal(element('#dialog-compare').disabled,true);
viewer.reset();
assert.equal(viewer.zoom,'fit');
assert.equal(viewer.referenceIndex,null);
console.log('photo viewer: pinned comparison, synced panning, aspect/edge geometry and resets passed');

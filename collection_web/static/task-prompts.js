'use strict';
// Prompt text and atomic Y-button stages; mirrored by tasks.compose_task.
globalThis.TaskPrompts=Object.freeze({
 modes:['shelf_grasp','tabletop','shelf_place'],
 infer(prompt){return /^Place the item held in the /i.test(prompt)?'tabletop':/^Place /i.test(prompt)?'shelf_place':'shelf_grasp';},
 compose(mode,hand,left='',right=''){
  if(!this.modes.includes(mode)||!['left','right','both'].includes(hand))throw Error('未知的提示词配置');
  const sides=hand==='both'?['left','right']:[hand],products={left:left.trim(),right:right.trim()};
  let prompt,stages;
  if(mode==='shelf_grasp'){
   prompt=hand==='both'?`Grasp ${products.left} with the left hand and then grasp ${products.right} with the right hand.`:`Grasp ${products[hand]} with the ${hand} hand.`;
   stages=sides.map(side=>`Grasp ${products[side]} with the ${side} hand.`);
   if(hand==='both')stages[1]=`Keep holding ${products.left} with the left hand and grasp ${products.right} with the right hand.`;
  }else if(mode==='tabletop'){
   const places=sides.map(side=>`Place the item held in the ${side} hand on the table.`);
   const grasps=sides.map(side=>`Grasp ${products[side]} from the table with the ${side} hand.`);
   stages=[...places,...grasps];
   prompt=hand==='both'?`Place the item held in the left hand on the table, then place the item held in the right hand on the table. Grasp ${products.left} from the table with the left hand, then grasp ${products.right} with the right hand.`:stages.join(' ');
  }else{
   stages=sides.map(side=>`Place ${products[side]} on the shelf with the ${side} hand.`);
   prompt=hand==='both'?`Place ${products.left} on the shelf with the left hand, then place ${products.right} with the right hand.`:stages[0];
   if(hand==='both')stages[1]=`Then place ${products.right} with the right hand.`;
  }
  return {task_name:prompt,stage_prompts:stages,subtask_num:stages.length,left:sides.includes('left')?products.left:'',right:sides.includes('right')?products.right:''};
 }
});

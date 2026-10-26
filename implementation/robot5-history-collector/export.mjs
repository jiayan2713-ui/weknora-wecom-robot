import fs from 'node:fs/promises';
import path from 'node:path';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';

const records = JSON.parse(await new Promise((resolve, reject) => {
  let value = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', part => { value += part; });
  process.stdin.on('end', () => resolve(value));
  process.stdin.on('error', reject);
}));
const output = process.argv[2];
if (!output) throw new Error('Output path is required');
const text = value => value == null ? null : String(value).startsWith('=') ? "'" + value : String(value);
const date = value => value ? new Date(new Date(value).getTime() + 8 * 3600000) : null;
const json = value => value == null ? null : JSON.stringify(value);
const column = index => {
  let value = '';
  for (let n = index + 1; n; n = Math.floor((n - 1) / 26)) value = String.fromCharCode(65 + (n - 1) % 26) + value;
  return value;
};
const overflow = value => typeof value === 'string' && value.length > 32767;
const parts = value => {
  if (!overflow(value)) return [];
  const chars = Array.from(value), result = [];
  for (let i = 0; i < chars.length; i += 15000) result.push(chars.slice(i, i + 15000).join(''));
  return result;
};
const referenceNames = refs => [...new Set((refs ?? []).map(ref =>
  ref.knowledge_title || ref.knowledge_filename || ref.knowledge_id || ref.id).filter(Boolean))].join('\n');
const referenceIds = refs => refs == null ? null : refs.map(ref => ({
  knowledge_title:ref.knowledge_title,knowledge_filename:ref.knowledge_filename,
  knowledge_id:ref.knowledge_id,chunk_id:ref.id,knowledge_base_id:ref.knowledge_base_id
}));

// Overflow is exceptional: the main table always keeps one row per QA. Entire
// text over Excel's cell limit is stored across extra columns in the detail row.
const extraFields = [
  {key:'question',label:'问题原文'}, {key:'answer',label:'回答原文'},
  {key:'reasoning_process',label:'推理过程'},
  {key:'reference_names',label:'引用资料名称'}, {key:'reference_ids',label:'引用资料标识JSON'},
  {key:'usage_json',label:'Token用量JSON'}
];
const data = records.map(r => ({...r,reference_names:referenceNames(r.references),
  reference_ids:json(referenceIds(r.references)),usage_json:json(r.usage)}));
for (const field of extraFields) field.count = Math.max(0,...data.map(r => parts(r[field.key]).length));
const cellText = (value, label) => overflow(value) ? `【${label}超过 Excel 单元格上限，完整内容见“采集明细”同序号行的${label}片段列】` : value;
const mainHeaders = ['时间（北京时间）','用户','问题','回答','状态','引用资料','推理过程'];
const mainRows = data.map(r => [date(r.question_at),r.user_id,cellText(r.question,'问题原文'),
  cellText(r.answer,'回答原文'),r.status,cellText(r.reference_names,'引用资料名称'),cellText(r.reasoning_process,'推理过程')]);
const detailHeaders = ['主表序号','记录ID','会话ID','请求ID','问题消息ID','回答消息ID',
  '回答更新时间（北京时间）','归属依据','引用资料标识JSON','Token用量JSON','渠道ID',
  '本地首次采集时间（北京时间）','本地最后变更时间（北京时间）','企微聊天ID','源会话删除时间（北京时间）'];
for (const field of extraFields) for (let i=1; i<=field.count; i++) detailHeaders.push(`${field.label}片段${i}`);
const detailRows = data.map((r,index) => [index+1,r.record_id,r.session_id,r.request_id,
  r.user_message_id,r.assistant_message_id,date(r.answer_updated_at),r.attribution,
  cellText(r.reference_ids,'引用资料标识JSON'),cellText(r.usage_json,'Token用量JSON'),r.channel_id,
  date(r.first_collected_at),date(r.last_changed_at),r.chat_id,date(r.session_deleted_at),
  ...extraFields.flatMap(field => {
    const values=parts(r[field.key]);
    return Array.from({length:field.count},(_,i) => values[i] ?? null);
  })]);

const wb = Workbook.create();
const literalRestores = [];
const sheets = [];
function addSheet(name,headers,rows,title,note,subtitle,widths,dateColumns,tableName) {
  const sheet = wb.worksheets.add(name);
  sheets.push(sheet);
  const sheetPath = `xl/worksheets/sheet${sheets.length}.xml`;
  const last = 5 + rows.length, end = column(headers.length-1);
  sheet.showGridLines = false;
  const matrix = [headers,...rows].map((row,index) => row.map((value,col) => {
    if (typeof value !== 'string') return value;
    if (value.startsWith("'=")) literalRestores.push({sheet:sheetPath,address:`${column(col)}${index+5}`,value});
    return text(value);
  }));
  sheet.getRange('A1').values = [[title]];
  sheet.getRange('A2').values = [[subtitle]];
  sheet.getRange('A3').values = [[note]];
  sheet.getRange(`A5:${end}${last}`).values = matrix;
  sheet.getRange(`A1:${end}${last}`).format.font = {name:'Arial',size:10,color:'#1F2937'};
  sheet.getRange('A1').format.font = {name:'Arial',size:14,bold:true};
  sheet.getRange(`A5:${end}5`).format = {fill:'#25476A',font:{name:'Arial',size:10,bold:true,color:'#FFFFFF'},rowHeight:30};
  sheet.getRange(`A5:${end}5`).format.horizontalAlignment='center';
  for (let i=0; i<headers.length; i++) sheet.getRange(`${column(i)}:${column(i)}`).format.columnWidth=widths[i] ?? 50;
  if (rows.length) {
    const body=sheet.getRange(`A6:${end}${last}`);
    body.format.verticalAlignment='top';
    body.format.wrapText=true;
    body.format.rowHeight=name==='问答记录' ? 120 : 45;
    for (const col of dateColumns) sheet.getRange(`${col}6:${col}${last}`).setNumberFormat('yyyy-mm-dd hh:mm:ss');
    sheet.tables.add(`A5:${end}${last}`,true,tableName);
  }
  sheet.freezePanes.freezeRows(5);
  return sheet;
}
addSheet('问答记录',mainHeaders,mainRows,'机器人 5 · 问答记录',
  '一组问答一行，原文不改写、不按显示长度拆行；较长内容可选中单元格，在 Excel 编辑栏展开查看。',
  `来源：本机 WeKnora / 机器人5企微渠道。共 ${records.length} 组问答；引用资料按名称去重展示。`,
  [23,22,48,100,18,52,100],['A'],'Robot5QA');
addSheet('采集明细',detailHeaders,detailRows,'机器人 5 · 采集明细',
  '主表序号对应“问答记录”数据行的顺序；记录ID用于稳定定位。问答原文在主表，超出单元格上限时全文在本页片段列。',
  '用于去重、来源追溯和故障排查。完整引用正文另存于本地 SQLite 和 JSON。',
  [12,40,40,40,40,40,23,24,65,45,40,23,23,40,23],['G','L','M','O'],'Robot5Collection');
wb.recalculate();
for (const name of ['问答记录','采集明细']) {
  await wb.inspect({kind:'table',range:`${name}!A5:F${Math.min(5+records.length,7)}`,include:'values',tableMaxRows:3,tableMaxCols:6,maxChars:500});
}
const file = await SpreadsheetFile.exportXlsx(wb);
const temp = output + '.tmp.xlsx';
await file.save(temp);
await fs.chmod(temp,0o600);
await fs.rename(temp,output);
// Narrow workaround for artifact-tool's leading "'=" literal interpretation.
await fs.writeFile(output+'.literal-restores.json',JSON.stringify(literalRestores),{mode:0o600});
if (process.argv.includes('--preview')) {
  for (const [name,filename] of [['问答记录','excel-preview.png'],['采集明细','detail-preview.png']]) {
    const blob = await wb.render({sheetName:name,range:`A1:${name==='问答记录'?'G':'F'}${Math.min(5+records.length,7)}`,scale:1,format:'png'});
    await fs.writeFile(path.join(path.dirname(output),filename),new Uint8Array(await blob.arrayBuffer()));
  }
  const sample = Math.max(0, data.findIndex(r => r.agent_steps?.some(step => step.tool_calls?.length) && !overflow(r.reasoning_process)));
  if (records.length) {
    const blob=await wb.render({sheetName:'问答记录',range:`G${sample+5}:G${sample+6}`,scale:1,format:'png'});
    await fs.writeFile(path.join(path.dirname(output),'reasoning-preview.png'),new Uint8Array(await blob.arrayBuffer()));
  }
}
process.stdout.write(JSON.stringify({records:records.length,excelRows:records.length,sheets:2,output})+'\n');

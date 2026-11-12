'use strict';

// The select retains the canonical English value; typing only filters candidates.
class ProductPicker {
 constructor(select) {
  this.select = select;
  this.root = document.createElement('div');
  this.root.className = 'product-picker';
  const id = select.id;
  this.root.innerHTML = `<div class="product-picker-control"><input id="${id}Search" type="text" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="${id}Options" autocomplete="off" spellcheck="false"><button type="button" class="product-picker-toggle" aria-label="展开${id === 'leftProduct' ? '左手' : '右手'}商品列表" aria-controls="${id}Options" aria-expanded="false" tabindex="-1">▾</button></div><div id="${id}Options" class="product-picker-options" role="listbox" aria-label="${id === 'leftProduct' ? '左手' : '右手'}候选商品" hidden></div><span class="product-picker-status" role="status" aria-live="polite"></span>`;
  select.after(this.root);
  select.hidden = true;
  this.input = this.root.querySelector('input');
  this.toggle = this.root.querySelector('button');
  this.list = this.root.querySelector('[role=listbox]');
  this.status = this.root.querySelector('[role=status]');
  this.active = -1;
  this.input.addEventListener('focus', () => {
   this.open('');
   this.input.select();
  });
  this.input.addEventListener('click', () => {
   if (this.list.hidden) { this.open(''); this.input.select(); }
  });
  this.input.addEventListener('input', () => this.open(this.input.value));
  this.input.addEventListener('keydown', event => this.keydown(event));
  // Keep the input focused while operating the arrow or choosing an option.
  this.toggle.addEventListener('mousedown', event => event.preventDefault());
  this.toggle.addEventListener('click', () => {
   if (!this.list.hidden) this.close();
   else { this.input.focus(); this.open(''); this.input.select(); }
  });
  this.list.addEventListener('mousedown', event => event.preventDefault());
  this.list.addEventListener('click', event => {
   const option = event.target.closest('[role=option]');
   if (option) this.choose(Number(option.dataset.index));
  });
  this.root.addEventListener('focusout', event => {
   if (!this.root.contains(event.relatedTarget)) this.close();
  });
  document.addEventListener('pointerdown', event => {
   if (!this.root.contains(event.target) && !this.list.hidden) this.close();
  });
  select.addEventListener('change', () => this.sync());
  this.sync();
 }

 sync() {
  this.input.value = this.select.selectedOptions[0]?.textContent || '';
  this.input.title = this.input.value;
 }

 setDisabled(disabled) {
  if (disabled && !this.list.hidden) this.close();
  this.input.disabled = disabled;
  this.toggle.disabled = disabled;
 }

 open(query) {
  if (this.input.disabled || this.select.disabled) return;
  const normalize = value => value.normalize('NFKC').toLowerCase().replace(/\s+/g, '');
  const needle = normalize(query);
  this.matches = Array.from(this.select.options).filter(option => normalize(option.textContent).includes(needle));
  this.list.replaceChildren();
  this.active = -1;
  this.input.removeAttribute('aria-activedescendant');
  this.matches.forEach((option, index) => {
   const row = document.createElement('div');
   row.id = `${this.select.id}Option${index}`;
   row.className = 'product-picker-option';
   row.setAttribute('role', 'option');
   row.setAttribute('aria-selected', String(option.value === this.select.value));
   row.dataset.index = index;
   row.textContent = option.textContent;
   this.list.append(row);
  });
  if (!this.matches.length) {
   const empty = document.createElement('div');
   empty.className = 'product-picker-empty';
   empty.textContent = '无匹配项';
   this.list.append(empty);
  }
  this.list.hidden = false;
  this.list.scrollTop = 0;
  this.input.setAttribute('aria-expanded', 'true');
  this.toggle.setAttribute('aria-expanded', 'true');
  this.status.textContent = `找到 ${this.matches.length} 个候选商品`;
 }

 close() {
  this.list.hidden = true;
  this.active = -1;
  this.input.setAttribute('aria-expanded', 'false');
  this.toggle.setAttribute('aria-expanded', 'false');
  this.input.removeAttribute('aria-activedescendant');
  this.status.textContent = '';
  this.sync();
 }

 choose(index) {
  if (this.input.disabled || this.select.disabled || !this.matches[index]) return;
  this.select.value = this.matches[index].value;
  this.close();
  this.select.dispatchEvent(new Event('change', {bubbles: true}));
 }

 keydown(event) {
  if (event.isComposing || event.keyCode === 229) return;
  if (event.key === 'Escape') { event.preventDefault(); this.close(); return; }
  if (event.key === 'Tab') { this.close(); return; }
  if (event.key === 'Enter') {
   event.preventDefault();
   if (!this.list.hidden) this.choose(this.active < 0 ? 0 : this.active);
   return;
  }
  if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
  event.preventDefault();
  if (this.list.hidden) this.open('');
  if (!this.matches.length) return;
  this.active = this.active < 0 ? (event.key === 'ArrowDown' ? 0 : this.matches.length - 1)
   : (this.active + (event.key === 'ArrowDown' ? 1 : -1) + this.matches.length) % this.matches.length;
  Array.from(this.list.children).forEach((row, index) => row.classList.toggle('active', index === this.active));
  const row = this.list.children[this.active];
  this.input.setAttribute('aria-activedescendant', row.id);
  row.scrollIntoView({block: 'nearest'});
 }
}

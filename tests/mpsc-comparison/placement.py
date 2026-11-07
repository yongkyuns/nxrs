"""RP2350 matched hot-operation placement using each OS startup copy mechanism."""
from pathlib import Path
import io,re,subprocess,tempfile,hashlib,json
from elftools.elf.elffile import ELFFile
AR = OBJCOPY = None
COMMON=re.compile(r'mpsc_comparison6engine(?:6worker|5offer|7payload)|mpsc_comparison8arrivals(?:4tick|5start|7release)|transport..(?:ordinary_std|Native)|^mp_(?:clock32|gate_post|gate_wait|queue_send|queue_receive|native_ticks|pulse_arm)$|^pulse_expired$|core..iter.*4next|core3num.*8div_ceil|mpsc_comparison5check|core4iter.*(?:7step_by|4next)|^OUTLINED_FUNCTION_|compiler_builtins.*mem|core5array8equality|mpsc_comparison8platform6native(?:10queue_send|13queue_receive|5clock|7irq_arm|8irq_post|9gate_wait|9gate_post|8priority|6policy)|^mem(?:cpy|move|set|cmp)$|^bcmp$|^__aeabi_mem(?:cpy|move|set|clr)')
NRUST=re.compile(r'std3sys12thread_local3key4racy7LazyKey5force|std3sys12thread_local2os12LocalPointer3get|std4sync4mpmc(?:5array|7counter)|std..sync..mpmc..array|std4sync4mpsc|std3sys4sync|__rust_nuttx_spin_|std6thread7current18current_or_unnamed|std6thread6Thread4park|std..thread..Thread|alloc11collections9vec_deque.*(?:13push_back_mut|9push_back|9pop_front)')
NATIVE=re.compile(r'^(?:nxsem_|nxsched_|wd_|sched_lock$|sched_unlock$|sched_idletask$|irq_dispatch$|pthread_getspecific$|tls_get_info$|nxsig_pendingset$|nxsig_nandset$|nxsig_addset$|sigemptyset$|sigisemptyset$|clock_systime|clock_get_sched_ticks|clock_increase_sched_ticks|up_switch_context$|up_fullcontextrestore$|up_saveusercontext$|arm_|exception_common)')
ZARCH=re.compile(r'^(?:arch_cpu_idle$|arch_irq_unlock_outlined$|z_arm_pendsv$|z_arm_svc$|z_arm_exc_exit$|z_arm_int_exit$|sys_clock_(?:isr|set_timeout|elapsed|cycle_get))')
def sections(data):
 e=ELFFile(io.BytesIO(data));tab=e.get_section_by_name('.symtab');names={}
 if tab:
  for s in tab.iter_symbols():
   if isinstance(s['st_shndx'],int):names.setdefault(s['st_shndx'],[]).append(s.name)
 return [(i,s.name,s.data(),s['sh_flags'],names.get(i,[])) for i,s in enumerate(e.iter_sections()) if s['sh_type']!='SHT_NOBITS' and s['sh_flags']&4]
def transform(path,kind,all_kernel=False):
 data=path.read_bytes()
 if not data.startswith(b'\x7fELF'):return None
 before=sections(data);mapping={}
 for i,name,raw,flags,names in before:
  if not name.startswith('.text'):continue
  if all_kernel or any(COMMON.search(n) or (kind=='nuttx' and (NRUST.search(n) or NATIVE.search(n))) or (kind=='zephyr' and ZARCH.search(n)) for n in names):mapping[name]=('.time_critical.' if kind=='nuttx' else '.ramfunc.')+name
 if not mapping:return None
 out=path.with_suffix(path.suffix+'.placed');args=[OBJCOPY]
 for old,new in mapping.items():args.extend(['--rename-section',old+'='+new])
 subprocess.run([*args,str(path),str(out)],check=True);after=sections(out.read_bytes());lookup={name:raw for _,name,raw,_,_ in after}
 assert len(before)==len(after)
 for _,name,raw,_,_ in before:assert lookup[mapping.get(name,name)]==raw,'instruction bytes changed'
 result=dict(object=path.name,sections=[dict(old=old,new=new,bytes=len(lookup[new]),sha256=hashlib.sha256(lookup[new]).hexdigest()) for old,new in mapping.items()],all_executable_section_bytes_preserved=True)
 out.replace(path);return result
def archive(path,kind,all_kernel=False):
 names=subprocess.check_output([AR,'t',str(path)],text=True).splitlines();assert len(names)==len(set(names)),('duplicate member names',path)
 assert all(Path(n).name==n and n not in ['.','..'] for n in names)
 with tempfile.TemporaryDirectory(prefix='mpsc-placement-') as d:
  d=Path(d);subprocess.run([AR,'x',str(path)],cwd=d,check=True);changed=[]
  for n in names:
   row=transform(d/n,kind,all_kernel)
   if row:changed.append(row)
  if not changed:return []
  target=d/'placed.a'
  for i in range(0,len(names),40):subprocess.run([AR,'rcD',str(target),*[str(d/n) for n in names[i:i+40]]],check=True,stdout=subprocess.DEVNULL)
  subprocess.run([AR,'s',str(target)],check=True);path.write_bytes(target.read_bytes());return changed


def prepare(root, prefix, env, zephyr=False):
 global AR, OBJCOPY
 AR, OBJCOPY = prefix+'ar', prefix+'objcopy'
 root=Path(root).resolve();kind='zephyr' if zephyr else 'nuttx';rows=[]
 if zephyr:
  archives=list((root/'firmware').rglob('*.a'))
  archives.append(root/'rust/thumbv8m.main-none-eabi/release/libmpsc_comparison.a')
 else:
  archives=list((root/'firmware/nuttx/staging').glob('*.a'))
 for path in archives:
  resolved=path.resolve()
  if not resolved.is_relative_to(root):raise ValueError('archive escapes owned build')
  changed=archive(resolved,kind,zephyr and path.name in ['libkernel.a','libdrivers__timer.a'])
  if changed:rows.append(dict(archive=str(path.relative_to(root)),objects=changed))
 if not rows:raise ValueError('no hot sections selected')
 (root/'hot-placement.json').write_text(json.dumps(dict(section_names_only=True,compiled_instruction_bytes_unchanged=True,selection=rows),indent=2)+'\n')
 if zephyr:
  subprocess.run(['cmake','--build',str(root/'firmware'),'--parallel','4'],env=env,check=True,stdout=subprocess.DEVNULL,timeout=300)
  elf=root/'firmware/zephyr/zephyr.elf'
 else:
  kernel=root/'firmware/nuttx'
  control=root/'link-vars.mk';control.write_text(".PHONY: mpsc-link-vars\nmpsc-link-vars:\n\t@echo '$(LINKLIBS)'\n")
  libs=subprocess.check_output(['make','-s','-C',str(kernel),'-f','Makefile','-f',str(control),'CROSSDEV='+prefix,'mpsc-link-vars'],env=env,text=True).strip()
  if not libs or not all(x.endswith('.a') and '/' not in x for x in libs.split()):raise ValueError('invalid native link libraries')
  elf=kernel/'nuttx';elf.unlink()
  with (root/'relink.log').open('w') as log:
   subprocess.run(['make','-C',str(kernel/'arch/arm/src'),'TOPDIR='+str(kernel),'APPDIR='+str(root/'firmware/apps'),'CROSSDEV='+prefix,'LINKLIBS='+libs,'-o','board/libboard.a','nuttx'],env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=300)
  subprocess.run([OBJCOPY,'-O','binary',str(elf),str(kernel/'nuttx.bin')],env=env,check=True)
 audit(elf,prefix,root/'hot-audit.json')

rootpat=re.compile(r'mpsc_comparison::engine::(?:worker|offer)|mpsc_comparison::arrivals::(?:tick|release)|transport::ordinary_std::Std.*Channel.*::send|^mp_(?:clock32|gate_post|gate_wait|queue_send|queue_receive|native_ticks|pulse_arm)$|^pulse_expired$|^std::sync::mpmc::array::(?:SendSlot|RecvSlot)|^std::sys::sync::thread_parking.*Parker::unpark$|^(?:arm_doirq|arm_svcall|arm_exception|exception_common|up_switch_context|nxsched_process_timer|sys_clock_isr|z_arm_pendsv|z_arm_svc|z_arm_exc_exit|z_arm_int_exit|arch_cpu_idle)$')
def cold(n):
 if any(x in n for x in ['panicking::','unwrap_failed','__assert','zassert_fail','k_panic','::fatal','mpsc_comparison::line','core::fmt::',' as core::fmt::','engine::enter','mp_get_schedule','_Unwind','std::thread::Thread::new','std::thread::current::init_current','std::thread::current::id::get_or_init','LazyKey::lazy_init','::configure','mp_tid','std::time::Instant','Thread::park_timeout','ThreadId::new','MutexGuard<u64>','pthread_mutex_lock','pthread_mutex_unlock','pthread_mutex_destroy','pthread_setspecific','Mutex::init','Mutex::lock::fail','__rust_alloc_zeroed','__rust_no_alloc_shim','handle_alloc_error','up_schedule_sigaction','z_do_kernel_oops','std::process::abort','thread_local::guard::key::enable','thread_local::os::LocalPointer::set','std::io::Write::write_fmt','drop_in_place<core::result::Result<(),std::io::error::Error>>']):return True
 if any(x in n for x in ['mpmc::list::','mpmc::zero::','VecDeque<T,A>::grow','__rust_dealloc']):return True
 return 'alloc::sync::Arc' in n and 'drop_slow' in n
def audit(elf,prefix,destination):
 out=subprocess.check_output([prefix+'objdump','-d','-C',str(elf)],text=True);fns={};current=None
 for line in out.splitlines():
  m=re.match(r'^([0-9a-f]+) <(.+)>:$',line)
  if m:current=int(m[1],16);fns[current]={'name':m[2],'edges':set()};continue
  if current is None:continue
  m=re.search(r'\b(?:blx?|b\.[wn])\s+([0-9a-f]+) <',line)
  if m:fns[current]['edges'].add(int(m[1],16))
  if 'veneer' in fns[current]['name']:
   m=re.search(r'\.word\s+0x([0-9a-f]+)',line)
   if m:fns[current]['edges'].add(int(m[1],16)&~1)
 todo=[a for a,f in fns.items() if rootpat.search(f['name'])];seen=set();parents={};outside=[];excluded=[]
 for a in todo:
  if not 0x20000000<=a<0x20082000:outside.append(dict(name=fns[a]['name'],address=hex(a),kind='root not SRAM'))
 while todo:
  a=todo.pop()
  if a in seen or a not in fns:continue
  seen.add(a);f=fns[a]
  if cold(f['name']):excluded.append(f['name']);continue
  if 0x10000000<=a<0x14000000:
   outside.append(dict(name=f['name'],address=hex(a),caller=fns[parents[a]]['name'] if a in parents else 'root'));continue
  for b in f['edges']:
   if b not in seen:parents[b]=a;todo.append(b)
 result=dict(elf_sha256=hashlib.sha256(elf.read_bytes()).hexdigest(),visited=len(seen),flash_boundaries=outside,excluded_cold=sorted(set(excluded)),indirect_calls_qualified=False)
 destination.write_text(json.dumps(result,indent=2)+'\n')
 if not seen or outside:raise ValueError('hot-path SRAM audit failed; inspect hot-audit.json')

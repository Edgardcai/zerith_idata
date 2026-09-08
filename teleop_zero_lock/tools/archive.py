"""Read and rebuild a local PyInstaller archive without running its code."""
import hashlib, marshal, struct, zlib
from pathlib import Path
MAGIC=b'MEI\014\013\012\013\016'
COOKIE='!8sIIII64s'
class Archive:
    def __init__(self,path):
        self.path=Path(path)
        with self.path.open('rb') as f:
            f.seek(0,2);self.size=f.tell();f.seek(max(0,self.size-1024*1024));tail=f.read()
            at=tail.rfind(MAGIC)
            if at<0:raise ValueError('PyInstaller archive cookie missing')
            self.cookie_at=self.size-len(tail)+at
            _,length,toc_offset,toc_size,self.python_version,self.python_lib=struct.unpack(COOKIE,tail[at:at+88])
            self.start=self.cookie_at+88-length
            self.suffix=tail[at+88:]
            f.seek(self.start+toc_offset);toc=f.read(toc_size)
        self.entries={};position=0
        while position<len(toc):
            n,offset,compressed,plain,flag,kind=struct.unpack('!IIIIBc',toc[position:position+18])
            if n<18:raise ValueError('Invalid archive entry')
            name=toc[position+18:position+n].rstrip(b'\0').decode()
            self.entries[name]=(offset,compressed,plain,flag,kind);position+=n
        self._pyz=None
    def raw(self,name):
        offset,size,*_=self.entries[name]
        with self.path.open('rb') as f:f.seek(self.start+offset);return f.read(size)
    def read(self,name):
        data=self.raw(name)
        return zlib.decompress(data) if self.entries[name][3] else data
    def modules(self):
        if self._pyz is None:
            name=next(n for n,e in self.entries.items() if e[-1]==b'z')
            self._pyz=self.read(name)
        return dict(marshal.loads(self._pyz[struct.unpack('!I',self._pyz[8:12])[0]:]))
    def code(self,name):
        if name in self.entries:return marshal.loads(self.read(name))
        _,offset,n=self.modules()[name]
        return marshal.loads(zlib.decompress(self._pyz[offset:offset+n]))
    def build(self,path,replacements):
        """Copy all original entries byte-for-byte except explicit replacements."""
        path=Path(path);temporary=path.with_suffix('.tmp');toc=[];offset=0
        with temporary.open('wb') as out,self.path.open('rb') as src:
            remaining=self.start
            while remaining:
                chunk=src.read(min(1024*1024,remaining));out.write(chunk);remaining-=len(chunk)
            for name,(_,_,plain,flag,kind) in self.entries.items():
                if name in replacements:
                    data=replacements[name];plain=len(data);data=zlib.compress(data) if flag else data
                else:data=self.raw(name)
                out.write(data);encoded=name.encode()+b'\0';n=(18+len(encoded)+15)//16*16
                toc.append(struct.pack('!IIIIBc',n,offset,len(data),plain,flag,kind)+encoded+b'\0'*(n-18-len(encoded)));offset+=len(data)
            table=b''.join(toc);out.write(table)
            out.write(struct.pack(COOKIE,MAGIC,offset+len(table)+88,offset,len(table),self.python_version,self.python_lib))
            out.write(self.suffix)
        check=Archive(temporary)
        for name in self.entries:
            if name in replacements:assert check.read(name)==replacements[name]
            else:assert check.entries[name][2:]==self.entries[name][2:] and check.raw(name)==self.raw(name),name
        temporary.chmod(0o700);temporary.replace(path)
        return hashlib.sha256(path.read_bytes()).hexdigest()

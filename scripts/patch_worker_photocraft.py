#!/usr/bin/env python3
"""Patch the user's existing Worker without resetting tokens or unrelated code."""
from pathlib import Path
import argparse


def patch(original:str,block:str)->str:
    if '// PHOTOCRAFT TELEGRAM MODULE v0.1 EXPERIMENTAL' in original:
        raise ValueError('Worker ja contem o modulo. Nao duplique o bloco.')
    markers=['\n// ---------- handlers ----------\n','async function handleCallback(cq) {','async function handleUpdate(update) {']
    for marker in markers:
        if original.count(marker)!=1: raise ValueError('Ponto de integracao ausente ou ambiguo: '+marker)
    start=original.index('async function handleUpdate(update) {')
    bind=original.find('  bindThread(msg);',start)
    if bind<0:raise ValueError('bindThread(msg) nao encontrado no handler.')
    cut=bind+len('  bindThread(msg);')
    out=original[:cut]+'\n  if (await pcHandleMessage(msg)) return;\n'+original[cut:]
    out=out.replace('async function handleCallback(cq) {','async function handleCallback(cq) {\n  if (cq.data?.startsWith("pc:")) return pcHandleCallback(cq);',1)
    return out.replace('\n// ---------- handlers ----------\n','\n'+block.rstrip()+'\n\n// ---------- handlers ----------\n',1)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--block',type=Path,default=Path(__file__).resolve().parents[1]/'worker-photocraft-bloco.js');a=p.parse_args()
    if a.input.resolve()==a.output.resolve():p.error('Use um arquivo de saida diferente para preservar backup.')
    result=patch(a.input.read_text(encoding='utf-8'),a.block.read_text(encoding='utf-8'))
    a.output.write_text(result,encoding='utf-8');print('Worker atualizado em '+str(a.output))

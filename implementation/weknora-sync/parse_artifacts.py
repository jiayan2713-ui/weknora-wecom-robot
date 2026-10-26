"""Typed, verified source/derived artifact references independent of job layout."""
import copy
from pathlib import Path
from docflow.common import WorkflowError, artifact, verify, fingerprint


class Artifacts:
    def __init__(self, base, work):
        self.roots = {'base': Path(base).resolve(), 'derived': (Path(work)/'derived-prepared').resolve()}
        self.roots['derived'].mkdir(parents=True, exist_ok=True)

    def bind(self, units, store='base'):
        result = copy.deepcopy(units)
        for unit in result:
            if not unit.get('image'):
                continue
            if 'image_ref' not in unit:
                root = self.roots[store]
                path = (root/unit['image']).resolve()
                if not path.is_relative_to(root):
                    raise WorkflowError('INTEGRITY_ERROR', '图像引用越出注册产物范围')
                unit['image_ref'] = {'store': store, **artifact(root, path)}
            unit.setdefault('recovery_parent', fingerprint({'label': unit['label'],
                'image': unit['image_ref']['sha256'], 'text': unit.get('text', '')}))
        return result

    def resolve(self, unit):
        ref = unit.get('image_ref')
        if not ref or ref.get('store') not in self.roots:
            raise WorkflowError('INTEGRITY_ERROR', '缺少已登记的图像引用')
        return verify(self.roots[ref['store']], ref)

    def add_store(self, name, root):
        self.roots[name] = Path(root).resolve()

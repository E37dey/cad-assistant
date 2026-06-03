"""
config.py — Configuration management.
"""
import json, os, time

class Config:
    def __init__(self):
        self.addin_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        self.config_path = os.path.join(self.addin_dir, '.ttc_config.json')
        self.history_path = os.path.join(self.addin_dir, '.ttc_history.json')
        self._data = self._load()

    def _load(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r') as f:
                    return json.load(f)
            except: pass
        return {}

    def _save(self):
        try:
            with open(self.config_path, 'w') as f:
                json.dump(self._data, f, indent=2)
        except: pass

    def get_api_key(self):
        return self._data.get('api_key') or os.environ.get('ANTHROPIC_API_KEY')

    def set_api_key(self, key):
        self._data['api_key'] = key
        self._save()

    def log_generation(self, prompt, metadata_json, success):
        entry = {
            'timestamp': time.time(), 'prompt': prompt,
            'metadata': metadata_json, 'success': success
        }
        history = self._load_history()
        history.append(entry)
        history = history[-100:]
        try:
            with open(self.history_path, 'w') as f:
                json.dump(history, f, indent=2)
        except: pass

    def _load_history(self):
        if os.path.exists(self.history_path):
            try:
                with open(self.history_path, 'r') as f:
                    return json.load(f)
            except: pass
        return []

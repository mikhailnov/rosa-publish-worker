require 'resque'
require 'yaml'
require 'erb'

Resque.redis = '192.168.1.2:6379'
Thread.abort_on_exception = true

ROOT = File.dirname(__FILE__) + '/../../../'

APP_CONFIG = YAML.load(ERB.new(File.read(File.join(ROOT, "config", "application.yml"))).result)
Dir.mkdir(APP_CONFIG['output_folder']) if !Dir.exist?(APP_CONFIG['output_folder'])
Dir.mkdir(ROOT + '/container') if !Dir.exist?(ROOT + '/container')

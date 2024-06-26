#!/usr/bin/python

# author mhakala
import json
import re
import subprocess
import tempfile
import os


def jobs_running():
   """find slurm-job-ids active on this node

   Returns list of Slurm JobIDs.  JobIDs are strings."""
   #data = subprocess.check_output(['squeue', '-w', os.uname()[1].split('.')[0], '-h', '-o', '%A'])
   data = subprocess.check_output(r"ps aux | grep [s]lurmstepd: | sed --regexp-extended 's/.*\[([0-9]+)\..*/\1/' | sort | uniq", shell=True)
   data = data.decode()
   return data.split()


def pid2id(pid):
   """Convert a process pid to slurm jobid"""
   with open('/proc/%s/cgroup' % pid) as f:
      for line in f:
         m = re.search('.*slurm.*\/job_(\d+)\/.*', line)
         if m:
            return m.group(1)
   return None


def job_info(jobs,current):
   """Get needed slurm values for each running job on the node

   For each jobid, update the `current` dictionary with the relevant values.
   """
   for job in jobs:
      output = subprocess.check_output(['scontrol', '-o', 'show', 'job', job])
      output = output.decode()
      cpus   = re.search('NumCPUs=(\d+)', output)
      tres   = re.search('TRES=(\S+)', output).group(1)
      nodes  = re.search('NumNodes=(\d+)', output)

      ngpu = 0
      for g in tres.split(','):
         gs = g.split('=')
         if gs[0] == 'gres/gpu':
            if len(gs) == 1:
               ngpu = 1
            else:
               ngpu = int(gs[-1])

      # drop multi-node jobs (will be added later if needed)
      if int(nodes.group(1)) > 1:
         del current[job]
      else:
         current[job]['ngpu'] = ngpu
         current[job]['ncpu']=int(cpus.group(1))

   return current


def gpu_info(jobinfo):
   """Scan nvidia-smi output, and update jobinfo dict in-place

   jobinfo is keyed by Slurm job IDs.  This is updated in-place.
   """
   import xml.etree.cElementTree as ET

   output = subprocess.check_output(['nvidia-smi', '-q', '-x'])
   output = output.decode()
   root = ET.fromstring(output)

   for gpu in root.findall('gpu'):
      procs = gpu.find('processes')
      mtot = 0.
      jobid = None
      # Here we assume that multiple job id's cannot access the same
      # GPU
      for pi in procs.findall('process_info'):
         pid = pi.find('pid').text
         jobid = pid2id(pid)
         # Assume used_memory is of the form '1750 MiB'. Needs fixing
         # if the unit is anything but MiB.
         mtot += float(pi.find('used_memory').text.split()[0])
      util = gpu.find('utilization')
      # Here assume gpu utilization is of the form
      # '100 %'
      gutil = float(util.find('gpu_util').text.split()[0])

      # power_draw is of the form 35.25 W
      power = gpu.find('gpu_power_readings')
      gpwrdraw = float(power.find('power_draw').text.split()[0])

      # only update, if jobid not dropped (multinode jobs)
      if jobid in jobinfo.keys():
         jobinfo[jobid]['gpu_util'] += gutil/jobinfo[jobid]['ngpu']
         jobinfo[jobid]['gpu_power'] += gpwrdraw
         jobinfo[jobid]['gpu_mem_max'] = max(mtot, jobinfo[jobid]['gpu_mem_max'])

   return jobinfo

def read_shm(fil):
   """Read exitsing jobstats file and return decoded json"""
   import os.path
   jobinfo = {}

   if(os.path.exists(fil)):
      with open(fil) as fp:
         jobinfo=json.loads(fp.read())

   return jobinfo


def write_shm(jobinfo, fname):
   """Write (+ encode) new jobstats file (atomic)"""
   with tempfile.NamedTemporaryFile(mode='w', delete=False, \
                     dir=os.path.dirname(os.path.normpath(fname))) as fp:
      json.dump(jobinfo, fp)
   os.chmod(fp.name, 0o0644)
   os.rename(fp.name, fname)

def main():
   import argparse
   parser = argparse.ArgumentParser()
   parser.add_argument('-n', '--nosleep', help="Don't sleep at the beginning",
                       action="store_true")
   parser.add_argument('fname', nargs='?', default='/run/gpustats.json',
                       help='Name of JSON file for reading/storing data')
   args = parser.parse_args()
   if not args.nosleep:
      import time
      import random
      time.sleep(random.randint(0, 30))

   # initialize stats
   current = {}
   jobs    = jobs_running()

   for job in jobs:
      current[job]={'gpu_util': 0, 'gpu_mem_max': 0, 'ngpu': 0, 'ncpu': 0, 'step': 1, 'gpu_power': 0 }

   # get current job info
   current = job_info(jobs, current)
   current = gpu_info(current)

   # combine with previous steps, calculate avgs and max
   prev = read_shm(args.fname)
   for job in jobs:
      if job in prev.keys():
         n = prev[job]['step']
         current[job]['gpu_util'] = ( float(prev[job]['gpu_util'])*n+float(current[job]['gpu_util']) )/(n+1)
         current[job]['gpu_power'] = ( float(prev[job]['gpu_power'])*n+float(current[job]['gpu_power']) )/(n+1)
         current[job]['gpu_mem_max']  = max(float(prev[job]['gpu_mem_max']), float(current[job]['gpu_mem_max']))
         current[job]['step'] = n+1

   # write json
   write_shm(current, args.fname)


if __name__ == '__main__':
    main()


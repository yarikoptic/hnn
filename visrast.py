import sys, os
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import pylab as plt
from neuron import h
from run import net
import paramrw
from filt import boxfilt
import spikefn
from math import ceil

# colors for the different cell types
dclr = {'L2_pyramidal' : 'g',
        'L5_pyramidal' : 'r',
        'L2_basket' : 'w', 
        'L5_basket' : 'b'}

ntrial = 0; tstop = -1; outparamf = spkpath = paramf = ''; EvokedInputs = OngoingInputs = False; drawindivrast = False

for i in range(len(sys.argv)):
  if sys.argv[i].endswith('.txt'):
    spkpath = sys.argv[i]
  elif sys.argv[i].endswith('.param'):
    paramf = sys.argv[i]
    tstop = paramrw.quickgetprm(paramf,'tstop',float)
    ntrial = paramrw.quickgetprm(paramf,'N_trials',int)
    EvokedInputs = paramrw.usingEvokedInputs(paramf)
    OngoingInputs = paramrw.usingOngoingInputs(paramf)
    outparamf = os.path.join('data',paramf.split('.param')[0].split(os.path.sep)[-1],'param.txt')
  elif sys.argv[i] == 'indiv':
    drawindivrast = True

extinputs = spikefn.ExtInputs(spkpath, outparamf)
extinputs.add_delay_times()

ncell = len(net.cells)

binsz = 5.0
smoothsz = 0 # no smoothing

# adjust input gids for display purposes
def adjustinputgid (extinputs, gid):
  if gid == extinputs.gid_prox:
    return 0
  elif gid == extinputs.gid_dist:
    return 1
  elif extinputs.is_prox_gid(gid):
    return 2
  elif extinputs.is_dist_gid(gid):
    return 3
  """
  if extinputs.is_evoked_gid(gid):
    if extinputs.is_prox_gid(gid):
      return extinputs.evprox_gid_range[0]
    else:
      return extinputs.evprox_gid_range[1] + 1 # evdist_gid_range[0]
  """
  return gid

def getdspk (fn):
  ddat = {}
  try:
    ddat['spk'] = np.loadtxt(fn)
  except:
    print('Could not load',fn)
    quit()
  dspk = {'Cell':([],[],[]),'Input':([],[],[])}
  dhist = {}
  for ty in dclr.keys(): dhist[ty] = []
  haveinputs = False
  for (t,gid) in ddat['spk']:
    ty = net.gid_to_type(gid)
    if ty in dclr:
      dspk['Cell'][0].append(t)
      dspk['Cell'][1].append(gid)
      dspk['Cell'][2].append(dclr[ty])
      dhist[ty].append(t)
    else:
      dspk['Input'][0].append(t)
      dspk['Input'][1].append(adjustinputgid(extinputs, gid))
      if extinputs.is_prox_gid(gid):
        dspk['Input'][2].append('r')
      elif extinputs.is_dist_gid(gid):
        dspk['Input'][2].append('g')
      else:
        dspk['Input'][2].append('w')
      haveinputs = True
  for ty in dhist.keys():
    dhist[ty] = np.histogram(dhist[ty],range=(0,tstop),bins=int(tstop/binsz))
    if smoothsz > 0:
      dhist[ty] = boxfilt(dhist[ty][0],smoothsz)
    else:
      dhist[ty] = dhist[ty][0]
  return dspk,haveinputs,dhist

def handle_close (evt): quit()

def drawhist (dhist,ax):
  ax2 = ax.twinx()
  fctr = 1.0
  if ntrial > 1:
    fctr = 1.0 / ntrial
  for ty in dhist.keys():
    plt.plot(np.arange(binsz/2,tstop+binsz/2,binsz),dhist[ty]*fctr,dclr[ty],linewidth=3,linestyle='--')
  ax2.set_xlim((0,tstop))
  ax2.set_ylabel('Spikes')

def drawrast (dspk, fig, sz=8, ltextra=''):
  lax = []
  lk = ['Cell']
  gdx = 111
  if haveinputs:
    lk.append('Input')
    gdx = 211
    lk.reverse()
  for i,k in enumerate(lk):
    ax = fig.add_subplot(gdx)
    lax.append(ax)
    ax.scatter(dspk[k][0],dspk[k][1],c=dspk[k][2],s=sz**2)
    plt.xlabel('Time (ms)');
    if k == 'Input':

      bins = ceil(150. * tstop / 1000.) # bins needs to be an int
      extinputs.plot_hist(ax,'dist',0,bins,(0,tstop),color='g',hty='step')
      extinputs.plot_hist(ax,'prox',0,bins,(0,tstop),color='r',hty='step')
      extinputs.plot_hist(ax,'evdist',0,bins,(0,tstop),color='g',hty='step')
      extinputs.plot_hist(ax,'evprox',0,bins,(0,tstop),color='r',hty='step')

      plt.ylabel('Input')
      ax.set_yticks([])

      red_patch = mpatches.Patch(color='red', label='dist')
      green_patch = mpatches.Patch(color='green', label='prox')
      plt.legend(handles=[red_patch,green_patch])

    else:
      plt.ylabel(k + ' ID')
      white_patch = mpatches.Patch(color='white', label='L2Basket')
      green_patch = mpatches.Patch(color='green', label='L2Pyr')
      red_patch = mpatches.Patch(color='red', label='L5Pyr')
      blue_patch = mpatches.Patch(color='blue', label='L5Basket')
      plt.legend(handles=[white_patch,green_patch,blue_patch,red_patch])
      ax.set_ylim((-1,ncell+1))
      ax.invert_yaxis()
    ax.set_facecolor('k')
    ax.grid(True)
    if tstop != -1: ax.set_xlim((0,tstop))
    if i ==0: ax.set_title('Raster Plot' + ' ' + ltextra)
    gdx += 1
  return lax

if __name__ == '__main__':
  plt.ion()
  if ntrial > 1:

    if drawindivrast:
      for i in range(ntrial):
        spkpathtrial = os.path.join('data',paramf.split('.param')[0].split(os.path.sep)[-1],'spk_'+str(i+1)+'.txt') 
        dspktrial,haveinputs,dhisttrial = getdspk(spkpathtrial) # show spikes from first trial
        fig = plt.figure(); 
        lax=drawrast(dspktrial,fig, 5, ltextra='Trial '+str(i+1)); drawhist(dhisttrial,lax[-1])

    fig = plt.figure(); fig.canvas.mpl_connect('close_event', handle_close)
    dspkall,haveinputs,dhistall = getdspk(spkpath) # histogram of spikes across trials
    lax = drawrast(dspkall,fig, 5, ltextra='All Trials')
    drawhist(dhistall,lax[-1])
  else:
    fig = plt.figure()
    fig.canvas.mpl_connect('close_event', handle_close)
    dspk,haveinputs,dhist = getdspk(spkpath)
    lax = drawrast(dspk,fig, 5)

